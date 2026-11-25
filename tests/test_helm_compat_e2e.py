"""
tests/test_helm_compat_e2e.py — helm_compat_check.py 통합 테스트

CLI 엔트리포인트를 클러스터 없이 --releases-json 주입으로 검증한다.
exit code 신뢰 모델(0/1/2)과 audit.log 기록을 확인한다.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "helm-k8s-compat" / "scripts" / "helm_compat_check.py"
REGISTRY = REPO / "helm-k8s-compat" / "registry"


def run_checker(tmp_path, releases, current, target, today="2026-06-26",
                pods=None, extra=()):
    """체커를 --releases-json 주입 모드로 실행하고 (returncode, audit내용)을 반환한다."""
    audit = tmp_path / "audit.log"
    proc = subprocess.run(
        [sys.executable, str(CHECKER),
         "--current", current, "--target", target,
         "--registry-dir", str(REGISTRY),
         "--audit-log", str(audit),
         "--today", today,
         "--releases-json", json.dumps(releases),
         *(["--pods-json", json.dumps({"items": pods})] if pods is not None else []),
         *map(str, extra)],
        capture_output=True, text=True)
    content = audit.read_text(encoding="utf-8") if audit.exists() else ""
    return proc.returncode, content, proc.stdout


class TestExitCodes:
    def test_window_chart_exceeds_kmax_blocks(self, tmp_path):
        # ingress-nginx 4.13.x → K8s 1.34로 올리면 CRITICAL (상한 1.33)
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "ingress-nginx",
              "chart": "ingress-nginx-4.13.5", "app_version": "1.13.5"}],
            current="1.33", target="1.34")
        assert rc == 1
        assert "HELM-SUPPORT" in audit
        assert "1.33 상한" in audit

    def test_supported_window_passes(self, tmp_path):
        # ingress-nginx 4.14.x → K8s 1.34 지원 범위 내. 단 retirement WARN은 남음
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "ingress-nginx",
              "chart": "ingress-nginx-4.14.2", "app_version": "1.14.2"}],
            current="1.33", target="1.34")
        # support는 PASS지만 lifecycle(retired)이 HIGH → exit 2
        assert rc == 2
        assert "retirement" in audit

    def test_no_releases_is_clean_pass(self, tmp_path):
        rc, audit, out = run_checker(tmp_path, [], current="1.33", target="1.34")
        assert rc == 0

    def test_unknown_chart_not_in_registry_is_info(self, tmp_path):
        # registry에 없는 차트 → 수동 검토 INFO, gate는 막지 않음(exit 0)
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "my-internal-app", "namespace": "default",
              "chart": "my-internal-app-2.1.0", "app_version": "2.1.0"}],
            current="1.33", target="1.34")
        assert rc == 0
        assert "my-internal-app" in audit
        assert "수동 검토" in audit


class TestK8sBreaks:
    def test_psp_removal_fires_on_125_jump(self, tmp_path):
        # ingress-nginx + 1.24→1.25 점프 → PSP 제거 HIGH 발화
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "ingress-nginx",
              "chart": "ingress-nginx-4.14.0", "app_version": "1.14.0"}],
            current="1.24", target="1.25")
        assert "PodSecurityPolicy" in audit or "PSP" in audit
        assert rc in (1, 2)


class TestAuditHeader:
    def test_audit_has_summary_line(self, tmp_path):
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "x",
              "chart": "ingress-nginx-4.14.2", "app_version": "1.14.2"}],
            current="1.33", target="1.34")
        assert "HELM-COMPAT" in audit or "Summary" in audit


class TestInputValidation:
    def test_invalid_version_string_rejected(self, tmp_path):
        # "1" 처럼 minor 없는 버전 → traceback 없이 명확한 에러 + 비정상 종료
        rc, audit, out = run_checker(
            tmp_path, [], current="1", target="1.34")
        assert rc != 0
        assert rc != 127  # helm 미존재와 구분

    def test_target_not_greater_than_current_rejected(self, tmp_path):
        # 다운그레이드/동일 버전 → 거부
        rc, audit, out = run_checker(
            tmp_path, [], current="1.34", target="1.33")
        assert rc != 0

    def test_equal_versions_are_audit_mode_not_error(self, tmp_path):
        # 동일 버전은 "지금 상태 점검" 모드다 — 차트를 올릴 때가 됐는지 주기적으로 보는 용도
        rc, audit, out = run_checker(
            tmp_path, [], current="1.34", target="1.34")
        assert rc == 0
        assert "상태 점검" in audit


class TestMalformedRelease:
    def test_unparseable_chart_ref_isolated_not_crash(self, tmp_path):
        # chart 필드가 "app-latest" 같이 버전 파싱 불가 → 해당 release만 격리, 크래시 없음
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "weird", "namespace": "default",
              "chart": "weird-latest", "app_version": "latest"}],
            current="1.33", target="1.34")
        # 크래시 없이 종료(0/1/2 중 하나), traceback 문자열 없음
        assert rc in (0, 1, 2)
        assert "Traceback" not in out


class TestK8sBreaksRequiresChartMin:
    def test_ingress_1_22_break_suppressed_for_modern_chart(self, tmp_path):
        # ingress-nginx 4.14.2로 1.21→1.25 점프 시, 1.22 Ingress v1beta1 제거
        # (requires_chart_min 4.0.0)는 이미 충족되므로 CRITICAL로 차단하면 안 된다.
        # 이 테스트의 핵심: v1beta1 CRITICAL이 억제되어 exit 1이 아니어야 함.
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "ingress-nginx",
              "chart": "ingress-nginx-4.14.2", "app_version": "1.14.2"}],
            current="1.21", target="1.25")
        assert "v1beta1" not in audit  # 억제되어 audit에 안 나타남
        assert rc != 1                  # CRITICAL 없음(HIGH들만 남아 exit 2)

    def test_ingress_1_22_break_fires_for_ancient_chart(self, tmp_path):
        # 반대: 구버전 차트 3.40.0은 requires_chart_min(4.0.0) 미충족 → CRITICAL 발화
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ingress-nginx", "namespace": "ingress-nginx",
              "chart": "ingress-nginx-3.40.0", "app_version": "0.49.0"}],
            current="1.21", target="1.25")
        assert "v1beta1" in audit
        assert rc == 1


class TestUsageErrorsAreNotGateVerdicts:
    """입력 오류가 WARN(2)으로 새어나가면 '검토 후 진행'으로 오해되어 게이트가 무력화된다."""

    def test_malformed_version_exits_64_not_2(self, tmp_path):
        rc, _, _ = run_checker(tmp_path, [], current="abc", target="1.36")
        assert rc == 64

    def test_downgrade_exits_64_not_2(self, tmp_path):
        rc, _, _ = run_checker(tmp_path, [], current="1.36", target="1.35")
        assert rc == 64

    def test_same_version_is_not_a_usage_error(self, tmp_path):
        # 동일 버전은 정기 점검 용도라 입력 오류가 아니다
        rc, _, _ = run_checker(tmp_path, [], current="1.36", target="1.36")
        assert rc != 64


class TestMinorPinAppVersionMissing:
    def test_missing_app_version_warns_not_blocks(self, tmp_path):
        # cluster-autoscaler chart 9.51.0은 app 1.x 체계 — chart 버전을 app으로 오인하면 안 됨
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "ca", "namespace": "kube-system",
              "chart": "cluster-autoscaler-9.51.0", "app_version": ""}],
            current="1.35", target="1.36")
        assert rc == 2
        assert "app 9.51" not in audit


class TestStalenessSurfacing:
    def test_stale_registry_adds_info_but_keeps_gate_open(self, tmp_path):
        # cert-manager 1.21.x는 K8s 1.36 지원 → PASS. 단 오래 지난 시점이면 근거 노후를 알린다.
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "cert-manager", "namespace": "cert-manager",
              "chart": "cert-manager-v1.21.0", "app_version": "v1.21.0"}],
            current="1.35", target="1.36", today="2027-06-01")
        assert rc == 0                    # INFO는 게이트를 막지 않는다
        assert "HELM-STALE" in audit

    def test_fresh_registry_has_no_stale_noise(self, tmp_path):
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "cert-manager", "namespace": "cert-manager",
              "chart": "cert-manager-v1.21.0", "app_version": "v1.21.0"}],
            current="1.35", target="1.36", today="2026-08-31")
        assert rc == 0
        assert "HELM-STALE" not in audit

    def test_app_layer_charts_stay_quiet(self, tmp_path):
        # K8s 확장 지점이 없는 앱 계층은 확인 기록만으로 조용히 통과한다
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "keycloak", "namespace": "keycloak",
              "chart": "keycloak-25.2.0", "app_version": "26.3.3"},
             {"name": "locust", "namespace": "locust",
              "chart": "locust-0.31.5", "app_version": "2.15.1"}],
            current="1.35", target="1.36", today="2026-08-31")
        assert rc == 0

    def test_charts_with_coupling_surface_still_warn(self, tmp_path):
        # 반대로 CRD·webhook을 가진 차트는 "문서에 상한이 없더라"로 통과시키지 않는다
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "keda", "namespace": "keda",
              "chart": "keda-2.18.0", "app_version": "2.18.0"}],
            current="1.35", target="1.36", today="2026-08-31")
        assert rc == 2
        assert "확장 지점" in audit


class TestCurrentStateAuditMode:
    """업그레이드 사전 점검 말고도, 차트를 올릴 때가 됐는지 주기적으로 보는 용도가 있다."""

    def test_same_version_is_audit_mode_not_usage_error(self, tmp_path):
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "keycloak", "namespace": "keycloak",
              "chart": "keycloak-25.2.0", "app_version": "26.3.3"}],
            current="1.35", target="1.35", today="2026-08-31")
        assert rc != 64
        assert "상태 점검" in audit

    def test_audit_mode_catches_already_violated_floor(self, tmp_path):
        # K8s를 올리지 않아도 지금 이미 지원 범위 밖이면 잡아야 한다
        rc, audit, out = run_checker(
            tmp_path,
            [{"name": "karpenter", "namespace": "karpenter",
              "chart": "karpenter-1.8.6", "app_version": "1.8.6"}],
            current="1.35", target="1.35", today="2026-08-31")
        assert rc == 1
        assert "1.9 이상" in audit

    def test_downgrade_still_rejected(self, tmp_path):
        rc, _, _ = run_checker(tmp_path, [], current="1.36", target="1.35")
        assert rc == 64


# H2/H4/H5/H6: offline observations use the same API objects as kubectl.
def make_pod(release="app", namespace="apps", ready=2, phase="Running", name="app-0"):
    return {"metadata": {"name": name, "namespace": namespace,
                         "labels": {"app.kubernetes.io/instance": release}},
            "spec": {"containers": [{"name": str(i)} for i in range(3)]},
            "status": {"phase": phase,
                       "containerStatuses": [{"name": str(i), "ready": i < ready}
                                             for i in range(3)]}}


def health_release():
    return {"name": "app", "namespace": "apps", "chart": "custom-1.0.0"}


class TestReleaseHealth:
    def test_ready_two_of_three_warns_without_baseline(self, tmp_path):
        rc, audit, out = run_checker(tmp_path, [health_release()], "1.33", "1.34",
                                    pods=[make_pod()])
        assert rc == 2
        assert "READY 2/3" in audit and "baseline 없음" in audit
        assert "NEXT:" in out.splitlines()[-1]

    @pytest.mark.parametrize("phase", ["Pending", "Failed", "Unknown"])
    def test_nonrunning_phase_warns_even_when_all_containers_ready(self, tmp_path, phase):
        rc, audit, _ = run_checker(tmp_path, [health_release()], "1.33", "1.34",
                                  pods=[make_pod(ready=3, phase=phase)])
        assert rc == 2 and phase in audit

    def test_missing_container_status_is_not_counted_as_ready(self, tmp_path):
        pod = make_pod(ready=3)
        pod["status"]["containerStatuses"].pop()
        rc, audit, _ = run_checker(tmp_path, [health_release()], "1.33", "1.34", pods=[pod])
        assert rc == 2 and "READY 2/3" in audit

    def test_namespace_and_instance_filter_excludes_unrelated_pods(self, tmp_path):
        rc, audit, _ = run_checker(
            tmp_path, [health_release()], "1.33", "1.34",
            pods=[make_pod(ready=3), make_pod(namespace="other"), make_pod(release="other")])
        assert rc == 0 and "HELM-HEALTH" not in audit

    def test_succeeded_all_ready_passes(self, tmp_path):
        rc, _, _ = run_checker(tmp_path, [health_release()], "1.33", "1.34",
                               pods=[make_pod(ready=3, phase="Succeeded")])
        assert rc == 0

    def test_existing_anomaly_info_new_anomaly_warn_and_worsening_warn(self, tmp_path):
        baseline = tmp_path / "health.json"
        release = health_release()
        rc, _, _ = run_checker(tmp_path, [release], "1.33", "1.34", pods=[make_pod()],
                               extra=["--health-out", baseline])
        assert rc == 2
        snapshot = json.loads(baseline.read_text())
        assert snapshot["releases"]["apps/app"]["unhealthy"] == [
            {"name": "app-0", "phase": "Running", "ready": "2/3"}]
        (tmp_path / "audit.log").unlink()
        rc, audit, _ = run_checker(tmp_path, [release], "1.34", "1.35", pods=[make_pod()],
                                   extra=["--health-baseline", baseline])
        assert rc == 0 and "기존 장애" in audit
        (tmp_path / "audit.log").unlink()
        rc, audit, _ = run_checker(tmp_path, [release], "1.34", "1.35",
                                   pods=[make_pod(), make_pod(name="app-1")],
                                   extra=["--health-baseline", baseline])
        assert rc == 2 and "신규 장애" in audit and "기존 장애" in audit
        rc, audit, _ = run_checker(tmp_path, [release], "1.34", "1.35",
                                   pods=[make_pod(ready=1)], extra=["--health-baseline", baseline])
        assert rc == 2 and "READY 1/3" in audit

    @pytest.mark.parametrize("pods", [[], [make_pod(namespace="other"), make_pod(release="other")]])
    def test_no_matching_pods_is_info_even_with_baseline(self, tmp_path, pods):
        baseline = tmp_path / "health.json"
        run_checker(tmp_path, [health_release()], "1.33", "1.34", pods=[],
                    extra=["--health-out", baseline])
        rc, audit, out = run_checker(tmp_path, [health_release()], "1.33", "1.34", pods=pods,
                                     extra=["--health-baseline", baseline])
        assert rc == 0 and "검사 범위 밖(일치 Pod 없음)" in audit
        assert "INFO" in out and "unknown" not in audit

    @pytest.mark.parametrize("payload", [{}, {"items": [None]}, {"items": [{}]}])
    def test_malformed_pod_input_stops_with_next(self, tmp_path, payload):
        proc = subprocess.run([sys.executable, str(CHECKER), "--current", "1.33",
                               "--target", "1.34", "--registry-dir", str(REGISTRY),
                               "--audit-log", str(tmp_path / "audit.log"),
                               "--releases-json", json.dumps([health_release()]),
                               "--pods-json", json.dumps(payload)], capture_output=True, text=True)
        assert proc.returncode == 64
        assert "Traceback" not in proc.stderr
        assert proc.stdout.splitlines()[-1].startswith("NEXT:")

    def test_malformed_baseline_is_not_treated_as_no_baseline(self, tmp_path):
        baseline = tmp_path / "bad.json"
        baseline.write_text('{"schema_version": 1, "releases": {"apps/app": {}}}')
        rc, _, out = run_checker(tmp_path, [health_release()], "1.33", "1.34",
                                 extra=["--health-baseline", baseline])
        assert rc == 64 and out.splitlines()[-1].startswith("NEXT:")


class TestRiskyValues:
    @pytest.mark.parametrize("value, expected", [(True, 2), (False, 0), (1, 0), ("true", 0)])
    def test_injected_values_work_without_live_reason_and_match_only_boolean_true(self, tmp_path, value, expected):
        release = {"name": "ci", "namespace": "build", "chart": "jenkins-5.0.0",
                   "values": {"controller": {"installLatestPlugins": value}}}
        rc, audit, _ = run_checker(tmp_path, [release], "1.33", "1.34")
        assert rc == expected
        assert "HELM-VALUES" in audit
        assert "risky_values 전용" in audit
        assert ("HELM-SUPPORT | PASS" not in audit)
        if expected == 0:
            assert "위험 규칙 불일치" in audit

    def test_absent_computed_values_path_is_info(self, tmp_path):
        release = {"name": "ci", "namespace": "build", "chart": "jenkins-5.0.0", "values": {}}
        rc, audit, _ = run_checker(tmp_path, [release], "1.33", "1.34")
        assert rc == 0 and "경로 없음" in audit

    def test_kiali_operator_has_own_compatibility_entry(self, tmp_path):
        release = {"name": "mesh", "namespace": "mesh", "chart": "kiali-operator-2.26.0"}
        rc, audit, _ = run_checker(tmp_path, [release], "1.33", "1.34")
        assert rc == 2 and "HELM-SUPPORT" in audit
        assert "registry 미등록" not in audit


class TestOfflineReleaseCompatibility:
    @pytest.mark.parametrize("without_cli", [False, True])
    @pytest.mark.parametrize("chart, app, expected", [
        ("custom-1.0.0", "1.0.0", 0),
        ("ingress-nginx-4.13.5", "1.13.5", 1),
        ("ingress-nginx-4.14.2", "1.14.2", 2)])
    def test_releases_only_preserves_original_exit_without_kubectl(
            self, tmp_path, monkeypatch, without_cli, chart, app, expected):
        if without_cli:
            monkeypatch.setenv("PATH", str(tmp_path))
        else:
            for cli in ("kubectl", "helm"):
                spy = tmp_path / cli
                spy.write_text("#!/bin/sh\necho called > '" + str(tmp_path / "called") + "'\nexit 1\n")
                spy.chmod(0o755)
            monkeypatch.setenv("PATH", str(tmp_path))
        release = {"name": "app", "namespace": "apps", "chart": chart, "app_version": app}
        rc, audit, out = run_checker(tmp_path, [release], "1.33", "1.34")
        assert rc == expected
        assert "health 미검사(pods 미주입)" in audit and "INFO" in out
        assert not (tmp_path / "called").exists()

    def test_missing_values_injection_remains_unknown_warn(self, tmp_path):
        release = {"name": "ci", "namespace": "build", "chart": "jenkins-5.0.0"}
        rc, audit, _ = run_checker(tmp_path, [release], "1.33", "1.34")
        assert rc == 2 and "설치 values를 수집하지 못함" in audit


@pytest.mark.parametrize("extra", [["--unexpected"], ["--releases-json", "{"],
                                   ["--releases-json", "{}"]])
def test_invalid_cli_and_release_json_exit_64(tmp_path, extra):
    proc = subprocess.run([
        sys.executable, str(CHECKER), "--current", "1.33", "--target", "1.34",
        "--registry-dir", str(REGISTRY), "--audit-log", str(tmp_path / "audit.log"),
        "--releases-json", "[]", *extra], capture_output=True, text=True)
    assert proc.returncode == 64
    assert proc.stdout.splitlines()[-1].startswith("NEXT:")
    assert "Traceback" not in proc.stderr
