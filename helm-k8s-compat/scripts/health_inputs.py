"""Validated offline inputs and read-only live observations (Python 3.9 stdlib)."""
import json
import re
import subprocess


def command_json(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None, "조회 명령 실패 (권한/인증/CLI 상태 확인)"
        return json.loads(result.stdout), None
    except FileNotFoundError:
        return None, "CLI 미존재"
    except subprocess.TimeoutExpired:
        return None, "조회 timeout"
    except json.JSONDecodeError:
        return None, "조회 결과가 JSON이 아님"


def validate_releases(releases):
    if not isinstance(releases, list):
        raise ValueError("release 입력은 JSON 배열이어야 함")
    seen = set()
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("release는 object여야 함")
        for field in ("name", "namespace", "chart"):
            value = release.get(field)
            if not isinstance(value, str) or not value or value.startswith("-"):
                raise ValueError(f"release {field}는 비어 있지 않은 문자열이어야 함")
        if not isinstance(release.get("app_version", ""), str):
            raise ValueError("release app_version은 문자열이어야 함")
        if "values" in release and not isinstance(release["values"], dict):
            raise ValueError("release values는 object여야 함")
        key = release["namespace"] + "/" + release["name"]
        if key in seen:
            raise ValueError(f"중복 release: {key}")
        seen.add(key)
    return releases


def validate_pods(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("pods 입력은 items 배열을 가진 Kubernetes PodList여야 함")
    for pod in data["items"]:
        if not isinstance(pod, dict):
            raise ValueError("Pod는 object여야 함")
        meta, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status", {})
        if not isinstance(meta, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
            raise ValueError("Pod metadata/spec/status는 object여야 함")
        if any(not isinstance(meta.get(k), str) or not meta[k] for k in ("name", "namespace")):
            raise ValueError("Pod name/namespace 누락")
        if not isinstance(meta.get("labels", {}), dict):
            raise ValueError("Pod labels는 object여야 함")
        containers = spec.get("containers")
        statuses = status.get("containerStatuses", [])
        if not isinstance(containers, list) or not containers or not isinstance(statuses, list):
            raise ValueError("Pod containers/containerStatuses 형식 오류")
        for container in containers + statuses:
            if not isinstance(container, dict) or not isinstance(container.get("name"), str):
                raise ValueError("container name 누락")
        if any(type(c.get("ready")) is not bool for c in statuses):
            raise ValueError("container ready는 boolean이어야 함")
        if not isinstance(status.get("phase", "unknown"), str):
            raise ValueError("Pod phase는 문자열이어야 함")
    return data["items"]


def validate_baseline(data):
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or
            not isinstance(data.get("releases"), dict)):
        raise ValueError("health baseline schema_version=1 및 releases object 필요")
    for key, health in data["releases"].items():
        if not isinstance(key, str) or len(key.split("/")) != 2 or not all(key.split("/")):
            raise ValueError("baseline release key는 namespace/name이어야 함")
        if (not isinstance(health, dict) or not isinstance(health.get("unhealthy"), list) or
                "unknown" not in health or
                (health["unknown"] is not None and not isinstance(health["unknown"], str))):
            raise ValueError("baseline unhealthy/unknown 형식 오류")
        for pod in health["unhealthy"]:
            if not isinstance(pod, dict) or set(pod) != {"name", "phase", "ready"}:
                raise ValueError("baseline Pod name/phase/ready 필요")
            if any(not isinstance(pod[k], str) or not pod[k] for k in pod):
                raise ValueError("baseline Pod 필드는 비어 있지 않은 문자열이어야 함")
            if not re.fullmatch(r"\d+/\d+", pod["ready"]):
                raise ValueError("baseline READY는 a/b이어야 함")
    return data


def observe_health(release, injected_pods, live=True):
    import compat_lib

    if injected_pods is None and not live:
        return {"unhealthy": [], "unknown": None, "info": "health 미검사(pods 미주입)"}
    if injected_pods is None:
        data, reason = command_json([
            "kubectl", "get", "pods", "-n", release["namespace"],
            "-l", "app.kubernetes.io/instance=" + release["name"], "-o", "json"])
        if reason:
            return {"unhealthy": [], "unknown": reason}
        try:
            pods = validate_pods(data)
        except ValueError as exc:
            return {"unhealthy": [], "unknown": str(exc)}
    else:
        pods = injected_pods
    selected = [p for p in pods if p["metadata"]["namespace"] == release["namespace"] and
                p["metadata"].get("labels", {}).get("app.kubernetes.io/instance") == release["name"]]
    if not selected:
        return {"unhealthy": [], "unknown": None, "info": "검사 범위 밖(일치 Pod 없음)"}
    return {"unhealthy": compat_lib.unhealthy_pods(selected, release), "unknown": None}
