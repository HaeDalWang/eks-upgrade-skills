# Helm K8s Compatibility — Reference

Output templates and remediation guidance for `helm_compat_check.py`.
The checker emits findings to the audit log; this file standardizes how the agent reports them.

---

## Finding Types

| rule_id | Axis | Typical severity | Meaning |
|---|---|---|---|
| `HELM-SUPPORT` | support | CRITICAL / HIGH / MEDIUM | Installed chart version vs target K8s support |
| `HELM-LIFECYCLE` | lifecycle | HIGH | Chart retired, or installed release past EOL |
| `HELM-K8SBREAK` | k8s_breaks | CRITICAL / HIGH | Removed/changed K8s API hit in the jump window |
| `HELM-CRD` | cross-cutting | HIGH | CRD-bearing chart needs a bump (Helm won't auto-update CRDs) |
| `HELM-HAZARD` | upgrade_hazards | HIGH | Manual step on chart bump (IAM, webhook, removed flag, …) |
| `HELM-HEALTH` | Pod health | HIGH / MEDIUM | New/changed anomaly or no baseline: WARN; identical baseline anomaly: INFO; unknown observation: WARN |
| `HELM-VALUES` | risky_values | HIGH / MEDIUM / LOW | Registered value match: WARN; explicit mismatch: PASS; unavailable values: unknown WARN; absent computed path: INFO |
| `HELM-UNKNOWN` | — | MEDIUM | Chart not in registry or risky_values-only entry — compatibility unknown, manual review required |

---

## Report Template (보고서)

### Korean Template (output_language: ko)

```
# Helm 호환성 점검 결과

## 점검 개요
- 대상: K8s {CURRENT} → {TARGET}
- 설치 Release: {N}개 | registry 등록 차트: {M}개
- 판정: {OPEN / WARN / BLOCKED}

## ❌ 차단 (CRITICAL)
- {chart} {version}: {detail}
  → 조치: {remediation}

## ⚠️ 확인 필요 (HIGH)
- {chart}: {detail}
  → 조치: {remediation}

## ℹ️ 수동 검토 (INFO)
- {chart} {version}: registry 미등록 — kubeVersion·릴리스 노트 직접 확인

## 다음 단계
{BLOCKED이면: 차단 항목 조치 후 재실행}
{WARN이면: 위 HIGH 항목을 검토하고 진행 여부 결정}
{OPEN이면: Helm 측 차단 요인 없음, 업그레이드 진행 가능}

감사 로그: {AUDIT_LOG_PATH}
```

### English Template (output_language: en)

```
# Helm Compatibility Check Result

## Overview
- Target: K8s {CURRENT} → {TARGET}
- Installed releases: {N} | registered charts: {M}
- Verdict: {OPEN / WARN / BLOCKED}

## ❌ Blocking (CRITICAL)
- {chart} {version}: {detail}
  → Action: {remediation}

## ⚠️ Review Required (HIGH)
- {chart}: {detail}
  → Action: {remediation}

## ℹ️ Manual Review (INFO)
- {chart} {version}: not in registry — check kubeVersion / release notes manually

## Next Steps
{If BLOCKED: resolve blocking items, then re-run}
{If WARN: review HIGH items and decide whether to proceed}
{If OPEN: no Helm-side blockers, safe to proceed}

Audit log: {AUDIT_LOG_PATH}
```

---

## Remediation Hints by Finding

Use these as the `→ 조치` line. Tailor to the specific chart from the finding detail.

| Finding | Remediation |
|---|---|
| `HELM-SUPPORT` window exceeded | Bump the chart to a version whose support window includes the target K8s, **before** upgrading the cluster. |
| `HELM-STALE` | The registry entry backing this PASS is older than 180 days (or has no `last_verified`). Not a defect — re-check `compat_source` and update `last_verified` if it still holds. |
| `HELM-SUPPORT` minor_pin mismatch | Upgrade the chart so its app minor equals the target K8s minor (e.g. cluster-autoscaler 1.34.x for K8s 1.34). |
| `HELM-SUPPORT` unknown | Open the chart's `compat_source` URL; confirm target-K8s support from official docs. Do not assume PASS. |
| `HELM-LIFECYCLE` whole_retired | Plan migration to the documented successor (e.g. Gateway API). Existing deploys keep working but receive no security fixes. |
| `HELM-LIFECYCLE` per_release EOL | Upgrade to a currently-supported release of the same chart. |
| `HELM-K8SBREAK` | The target removes/changes an API this chart relies on. Bump the chart to the version that supports the new API. |
| `HELM-CRD` | Helm v3 does not auto-update CRDs. Run `kubectl apply` on the chart's CRDs before/after `helm upgrade`. |
| `HELM-HAZARD` | Follow the chart's UPGRADE notes for the specific version jump (IAM policy, webhook, removed flags). |
| `HELM-UNKNOWN` | Inspect `helm show chart <repo>/<name>` kubeVersion and the project's release notes manually. |

---

## When the Checker Cannot Run

| Symptom | Cause | Response |
|---|---|---|
| exit `127` | `helm` not installed or no cluster access | Report plainly; ask user to install helm / fix kubeconfig. **Not a pass.** |
| exit `64` | Invalid CLI/JSON/baseline | **Not a gate verdict.** Fix the invocation and re-run. Never treat as WARN. Same-version checks are supported. |
| exit `1` + ERROR | Output write failure | Stop, fix output access and re-run; final `NEXT:` provides the action. |
| 0 registered charts loaded | `--registry-dir` wrong or empty | Verify the path points at `helm-k8s-compat/registry/`. |

Never convert an inability-to-check into an implicit "compatible." Surface the gap to the user.


## Health and Values Inputs / Verdicts

| Option | Format / purpose |
|---|---|
| `--pods-json JSON` | Kubernetes PodList (`{"items":[...]}`), alternative to namespace + instance-label `kubectl get pods` per release |
| `--health-out FILE` | JSON `schema_version: 1`, `releases: {"namespace/release": {"unhealthy": [{"name": "pod", "phase": "Running", "ready": "2/3"}], "unknown": null}}` |
| `--health-baseline FILE` | Previously saved snapshot, same cluster; missing release means its current anomalies are new |
| `--releases-json JSON` | Existing release array, optional `values` object supplies computed values including chart defaults, equivalent to `helm get values --all`; unprovided values stays unknown WARN, absent path is INFO; without `--pods-json`, no kubectl is called and health is INFO (health 미검사(pods 미주입)) |

| Observation | Verdict |
|---|---|
| Phase outside Running/Succeeded OR READY a != b | Unhealthy, including Running 2/3 and missing container status |
| Identical Pod name + phase + READY in baseline | INFO, 기존 장애 (exit 0 if no other warning/blocker) |
| New/changed anomaly | WARN, 신규 장애 (exit 2 unless another rule blocks) |
| Anomaly without baseline | WARN, 기존/신규 unknown |
| Failed live observation | WARN, unknown + reason; never suppressed by baseline |
| No matching Pods | INFO, 검사 범위 밖(일치 Pod 없음); no exit effect |
| Offline releases without injected Pods | INFO, health 미검사(pods 미주입); no live Pod lookup or exit effect |
| Absent path in computed values | INFO, 경로 없음; no exit effect |
| Explicit risky_values typed equality | WARN, chart-specific reason; only Jenkins controller.installLatestPlugins=true initially |
| Explicit risky_values mismatch | PASS for that rule, independent of compatibility findings |

Collection is read-only. `helm get values <release> -n <ns> --all -o json` collects computed values including chart defaults; absent paths are INFO (경로 없음). Collection failures remain unknown WARN. Only registered paths are inspected; values are never written to snapshots or audit logs. Pod names are used as identity, so replacements with different names count as new anomalies. Snapshots do not authenticate cluster identity: never compare across clusters. Health comparison does not change compatibility/lifecycle verdicts. No instance-labelled Pods is outside inspection scope (INFO), including charts that only create CRDs or manage Pods under other labels. Only live release listing enables live Pod lookup.

Health snapshots may include an `info` string explaining skipped/out-of-scope inspection; it never suppresses an unknown live observation.
