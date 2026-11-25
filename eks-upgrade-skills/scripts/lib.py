#!/usr/bin/env python3
"""
lib.py — gate_check.py / phase_gate.py 공통 헬퍼 모듈

색상 상수, 시스템 상수, GateResult, audit 함수, record, CLI 헬퍼,
리소스 파서를 단일 모듈로 제공한다.

Python 3.9+ stdlib only — 서드파티 의존성 없음.
"""

import argparse
import fcntl
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DIAGNOSTIC_TARGET_LIMIT = 10
SECRET_KEY = re.compile(r"(^|_)(password|secret|private_key)($|_)", re.I)


def diagnostic_targets(items: list) -> str:
    targets = list(dict.fromkeys(items))
    extra = len(targets) - DIAGNOSTIC_TARGET_LIMIT
    return ", ".join(targets[:DIAGNOSTIC_TARGET_LIMIT]) + (f" 외 {extra}개" if extra > 0 else "")


def stalled_statefulsets(data: dict) -> list:
    """Only compare known revisions/replicas; absent status is not rollout evidence."""
    stalled = []
    for item in data.get("items", []):
        status, spec = item.get("status", {}), item.get("spec", {})
        current, update = status.get("currentRevision"), status.get("updateRevision")
        ready, replicas = status.get("readyReplicas", 0), spec.get("replicas")
        if current and update and current != update and replicas is not None and ready < replicas:
            stalled.append(item)
    return stalled


def redact_plan(value, sensitive=False):
    """Copy JSON for display/storage; never use redacted data to authorize apply."""
    if sensitive is True:
        return "***"
    if isinstance(value, dict):
        masks = sensitive if isinstance(sensitive, dict) else {}
        result = {}
        for key, item in value.items():
            mask = masks.get(key, False)
            if SECRET_KEY.search(key):
                mask = True
            elif mask is not True and key in ("values", "before", "after", "value", "default"):
                marker = {"values": "sensitive_values", "value": "sensitive", "default": "sensitive"}.get(key, key + "_sensitive")
                mask = value.get(marker, mask)
            result[key] = redact_plan(item, mask)
        return result
    if isinstance(value, list):
        masks = sensitive if isinstance(sensitive, list) else []
        return [redact_plan(item, masks[i] if i < len(masks) else False)
                for i, item in enumerate(value)]
    return value


def redact_plan_output(plan):
    """Mask plan paths before deriving or rendering diagnostics."""
    safe = redact_plan(plan)
    root = (plan.get("configuration") or {}).get("root_module") or {}
    for name, declaration in (root.get("variables") or {}).items():
        if isinstance(declaration, dict) and declaration.get("sensitive") is True:
            if name in (safe.get("variables") or {}):
                safe["variables"][name] = {"value": "***"}
    return safe


# ══════════════════════════════════════════════════════════════
# 색상 상수 (터미널)
# ══════════════════════════════════════════════════════════════
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
NC = "\033[0m"

# ══════════════════════════════════════════════════════════════
# 시스템 상수
# ══════════════════════════════════════════════════════════════

# 시스템 네임스페이스 (WLS-002, WLS-004, WLS-005, WLS-006 제외 대상)
# 이 네임스페이스의 워크로드는 사용자 워크로드로 분류하지 않음
SYSTEM_NS = frozenset({
    # Kubernetes 기본
    "kube-system", "kube-node-lease", "kube-public",
    # AWS 관리형 컴포넌트
    "karpenter", "amazon-cloudwatch", "amazon-guardduty", "aws-secrets-manager",
    # 일반적으로 사용되는 인프라 컴포넌트
    "cert-manager", "external-secrets", "external-dns",
    "ingress-nginx", "aws-load-balancer-controller",
    "istio-system", "istio-ingress",
    "monitoring", "logging", "observability",
    "argocd", "flux-system",
    "velero", "kyverno",
})

# INF-004 Data Plane 리소스 타입
DATA_PLANE_RESOURCES: frozenset = frozenset({
    "aws_eks_node_group",
    "aws_launch_template",
    "aws_autoscaling_group",
    "aws_eks_fargate_profile",  # Fargate 프로파일 recreate는 실행 중 Pod 즉시 종료
})

# COM-003 비정상 Add-on 상태
ADDON_BAD_STATES: frozenset = frozenset({
    "DEGRADED",
    "CREATE_FAILED",
    "UPDATE_FAILED",   # 업데이트 실패 상태도 업그레이드 전 차단
    "DELETE_FAILED",   # 삭제 실패 상태도 비정상으로 처리
})


# ══════════════════════════════════════════════════════════════
# GateResult: 검증 상태를 캡슐화하는 클래스 (글로벌 변수 대체)
# ══════════════════════════════════════════════════════════════
@dataclass
class GateResult:
    """Gate 검증 결과를 캡슐화. 글로벌 mutable state 대신 인스턴스 사용."""
    critical_fail: int = 0
    high_warn: int = 0
    medium_info: int = 0
    total_pass: int = 0
    total_rules: int = 0
    audit_lines: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """모든 카운터와 감사 로그 초기화."""
        self.critical_fail = 0
        self.high_warn = 0
        self.medium_info = 0
        self.total_pass = 0
        self.total_rules = 0
        self.audit_lines.clear()


# ══════════════════════════════════════════════════════════════
# 모듈 레벨 _gate 인스턴스 — 단일 진실 원천
# ══════════════════════════════════════════════════════════════

_gate = GateResult()


def reset_gate() -> None:
    """글로벌 Gate 상태 초기화."""
    _gate.reset()


# ══════════════════════════════════════════════════════════════
# 감사 로그
# ══════════════════════════════════════════════════════════════
def parse_audit_line(line: str) -> Optional[dict]:
    """Parse current five-column and historical four-column records.

    The detail may itself contain ` | `; comments and malformed lines are ignored.
    """
    fields = line.rstrip("\n").split(" | ", 4)
    if len(fields) < 4 or fields[0].startswith("#"):
        return None
    results = {"PASS", "FAIL", "WARN", "SKIP", "INFO", "ENTER", "START", "STOP"}
    severities = {"-", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
    if len(fields) == 5 and fields[2] in severities and fields[3] in results:
        timestamp, rule_id, severity, result, detail = fields
    else:
        timestamp, rule_id, result, detail = line.rstrip("\n").split(" | ", 3)
        severity = "-"
    if result not in results:
        return None
    return dict(timestamp=timestamp, rule_id=rule_id, severity=severity,
                result=result, detail=detail)


def baseline_values(path: str, rule_id: str, scope: dict, field: str) -> Optional[set]:
    """Read the latest matching script baseline; invalid evidence never grants a waiver."""
    latest = None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        row = parse_audit_line(line)
        if not row or row["rule_id"] != rule_id:
            continue
        try:
            values = dict(token.split("=", 1) for token in shlex.split(row["detail"]))
        except ValueError:
            latest = None
            continue
        if any(values.get(key) != value for key, value in scope.items()):
            continue
        latest = None
        if row["result"] != "INFO" or field not in values:
            continue
        try:
            items = (json.loads(values[field]) if field == "addresses"
                     else values[field].split(",") if values[field] else [])
            if isinstance(items, list) and all(isinstance(item, str) and item
                                               and item != "unknown" for item in items):
                latest = set(items)
        except ValueError:
            continue
    return latest


def skill_revision() -> str:
    """Identify the actual skill checkout, never the caller's Terraform repo."""
    skill_dir = Path(__file__).resolve().parent.parent
    result = run_cmd(["git", "-C", str(skill_dir), "rev-parse", "--short", "HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    for name in ("VERSION", ".install-version"):
        marker = skill_dir / name
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def _audit_line(rule_id: str, result: str, detail: str,
                severity: Optional[str] = None) -> str:
    severity = severity or {"FAIL": "CRITICAL", "WARN": "HIGH"}.get(result, "-")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = [now, rule_id, severity, result, detail]
    return " | ".join(str(value).replace("\r", "\\r").replace("\n", "\\n")
                      for value in fields)


def audit_init(cluster_name: str, current_ver: str, target_ver: str) -> None:
    """audit 헤더 작성. phase_gate는 current_ver를 모를 때 빈 문자열을 넘기는데,
    그대로 두면 `# Upgrade:  → 1.34`처럼 헤더가 오염된다. 빈 값을 graceful하게 처리한다."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cluster_line = f"# Cluster: {cluster_name}" if cluster_name else "# Cluster: (n/a — phase gate)"
    if current_ver and target_ver:
        version_line = f"# Upgrade: {current_ver} → {target_ver}"
    elif target_ver:
        version_line = f"# Target version: {target_ver}"
    else:
        version_line = "# Version: (n/a — phase gate)"
    _gate.audit_lines.extend([
        "# K8s Upgrade Pre-flight Audit Log",
        "# Generated by gate_check.py (deterministic)",
        cluster_line,
        version_line,
        f"# Started: {now}",
        f"# Skill commit: {skill_revision()}",
        "# ──────────────────────────────────────────",
    ])


def audit_write(rule_id: str, result: str, detail: str,
                severity: Optional[str] = None) -> None:
    _gate.audit_lines.append(_audit_line(rule_id, result, detail, severity))


def audit_flush(path: str) -> None:
    """Summarize records in this block, including direct phase audit_write calls."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [row for line in _gate.audit_lines if (row := parse_audit_line(line))
            and row["result"] not in {"ENTER", "START", "STOP", "SKIP"}
            and not row["rule_id"].removeprefix("PHASE").isdigit()]
    fails = sum(row["result"] == "FAIL" and row["severity"] in {"CRITICAL", "-"}
                for row in rows)
    warns = sum(row["result"] == "WARN" or
                (row["result"] == "FAIL" and row["severity"] == "HIGH") for row in rows)
    info = sum(row["result"] == "FAIL" and row["severity"] in {"MEDIUM", "LOW"}
               for row in rows)
    passed = sum(row["result"] == "PASS" for row in rows)
    gate = "BLOCKED" if fails else ("WARN" if warns else "OPEN")
    header_end = next((i for i, line in enumerate(_gate.audit_lines)
                       if not line.startswith("#")), len(_gate.audit_lines))
    lines = (_gate.audit_lines[:header_end] +
             [f"# Executed rules: {len({row['rule_id'] for row in rows})}"] +
             _gate.audit_lines[header_end:])
    lines.extend([
        "# ──────────────────────────────────────────",
        f"# Summary: CRITICAL_FAIL={fails} HIGH_WARN={warns} MEDIUM_INFO={info} PASS={passed}/{len(rows)}",
        f"# Gate: {gate}",
        f"# Finished: {now}",
    ])
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write("\n".join(lines) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    _gate.audit_lines.clear()


def audit_append(path: str, rule_id: str, result: str, detail: str,
                 severity: Optional[str] = None) -> None:
    """Append one record under the same lock used by buffered gate writers."""
    line = _audit_line(rule_id, result, detail, severity) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)


# ══════════════════════════════════════════════════════════════
# 결과 기록
# ══════════════════════════════════════════════════════════════
def record(rule_id: str, severity: str, result: str, detail: str) -> None:
    _gate.total_rules += 1

    if result == "PASS" or result == "SKIP":
        _gate.total_pass += 1
        icon = f"{GREEN}✅ PASS{NC}" if result == "PASS" else f"{CYAN}⏭️  SKIP{NC}"
        print(f"{icon}  {rule_id:<8s} {detail}")
    elif result == "FAIL":
        if severity == "CRITICAL":
            _gate.critical_fail += 1
            print(f"{RED}❌ FAIL{NC}  {rule_id:<8s} [{severity}] {detail}")
        elif severity == "HIGH":
            _gate.high_warn += 1
            print(f"{YELLOW}⚠️  WARN{NC}  {rule_id:<8s} [{severity}] {detail}")
        else:
            # MEDIUM / LOW — 보고만, Gate 판정에 영향 없음
            _gate.medium_info += 1
            print(f"{CYAN}ℹ️  INFO{NC}  {rule_id:<8s} [{severity}] {detail}")

    audit_write(rule_id, result, detail, severity)


# ══════════════════════════════════════════════════════════════
# CLI 헬퍼
# ══════════════════════════════════════════════════════════════
class GateArgumentParser(argparse.ArgumentParser):
    """Usage errors are FAIL (1); the final stdout line always gives NEXT."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print("{}: error: {}".format(self.prog, message), file=sys.stderr)
        print("NEXT: STOP invalid arguments: " + message.replace("\n", " "))
        self.exit(1)


def run_cmd(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """subprocess wrapper — 실패 시 빈 stdout 반환."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(e))


def kubectl_json(resource: str, all_ns: bool = True, timeout: int = 60) -> Optional[dict]:
    """kubectl get <resource> -o json 실행 후 dict 반환. 실패 시 None 반환."""
    cmd = ["kubectl", "get", resource, "-o", "json"]
    if all_ns:
        cmd.insert(2, "-A")
    r = run_cmd(cmd, timeout=timeout)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ══════════════════════════════════════════════════════════════
# 리소스 파서
# ══════════════════════════════════════════════════════════════
def _parse_cpu(s: str) -> int:
    """CPU 문자열 → 밀리코어."""
    if s is None or not s:
        return 0
    if s.endswith("m"):
        return int(s[:-1])
    try:
        return int(float(s) * 1000)
    except ValueError:
        return 0


def _parse_mem(s: str) -> int:
    """메모리 문자열 → MiB."""
    if s is None or not s:
        return 0
    if s.endswith("Ki"):
        return int(s[:-2]) // 1024
    if s.endswith("Mi"):
        return int(s[:-2])
    if s.endswith("Gi"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("M"):
        return int(s[:-1]) * 1_000_000 // (1024 * 1024)
    try:
        return int(s) // (1024 * 1024)
    except ValueError:
        return 0
