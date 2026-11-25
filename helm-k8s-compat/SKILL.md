---
name: helm-k8s-compat
description: >
  Pre-upgrade compatibility check for Helm-installed charts against a target Kubernetes version.
  Detects chart-level upgrade blockers that EKS Insights and kubent cannot see — supported-version
  windows, retired/EOL charts, K8s API-removal breakage, and manual CRD/IAM/webhook upgrade hazards.
  Uses a curated registry of de-facto-standard charts plus live `helm` inspection.
  Trigger keywords: 'helm compatibility', 'helm chart k8s version', 'chart upgrade readiness',
  'helm 호환성', '차트 호환성', 'helm 버전 점검'
---

# Helm ↔ Kubernetes Compatibility Check

Verify that Helm-installed charts will survive a Kubernetes minor-version upgrade **before** it runs.

This skill answers the question EKS Insights and kubent cannot: *"Will my installed charts' controllers
keep working on the target version, and is my chart version even supported there?"* That knowledge lives
in release notes and support matrices, not in live API scans.

The verdict is produced by a deterministic Python script (`helm_compat_check.py`) whose exit code is the
single source of truth — the LLM interprets and reports, but cannot override the gate.

All scripts are in `./scripts/`. The curated knowledge base is in `./registry/`.
---

## When to Use

- As a **Phase 0 supplement** during a Kubernetes/EKS upgrade — pass the current and target versions.
- As a **periodic health check** — pass the *same* version for both (`--current 1.35 --target 1.35`).
  This answers "are my charts still in a supported range, and is anything past EOL?" without an
  upgrade in sight. It catches charts that have already drifted out of support on the version you
  are running today.

Only a downgrade (`target < current`) is rejected.

This skill is IaC-agnostic — it inspects Helm releases, not Terraform/CDK/etc. It works whether or not
the main upgrade skill is installed.

---

## Prerequisites

| Requirement | Purpose |
|---|---|
| `helm` CLI (v3+) with cluster access | List releases and read registered risky values (`helm get values --all`) |
| `kubectl` with Pod read access | Inspect release Pods within each namespace |
| `python3` (3.9+) | Run the checker (stdlib only, no third-party deps) |
| Current + target K8s version | e.g. `1.33` → `1.34` |

> The checker can also run **without a cluster** via `--releases-json` injection (optional `--pods-json` for health) (testing / dry-run).

---

## Step 1: Collect Inputs

Determine the current and target Kubernetes **minor** versions.

- If invoked from the main upgrade skill: reuse `current_version` / `target_version` from the recipe.
- If standalone: ask the user.

```
Helm 호환성 점검을 위해 알려주세요:

1. 현재 Kubernetes 버전 (예: 1.33)
2. 대상 Kubernetes 버전 (예: 1.34)
```

> One minor step at a time (1.33 → 1.34). For multi-step upgrades, run the check once per step.

---

## Step 2: Run the Compatibility Check

Run from the skill root:

```bash
python3 scripts/helm_compat_check.py \
  --current <CURRENT_VERSION> \
  --target <TARGET_VERSION> \
  --registry-dir ./registry \
  --audit-log <AUDIT_LOG_PATH>
```

The script does the following automatically:
1. `helm ls -A -o json` to enumerate installed releases.
2. Match each release against the curated `registry/` by chart name.
3. Evaluate three orthogonal axes plus K8s API-removal events (see `registry/_schema.md`):
   - **support** — does the installed chart version support the target K8s? (`window` / `k8s_floor` / `minor_pin` / `unknown`)
   - **freshness** — was this registry entry verified recently enough to trust a PASS? (`HELM-STALE`, INFO only)
   - **lifecycle** — is the chart retired, or is the installed release past its EOL date?
   - **upgrade_hazards** — if the chart must be bumped, what manual steps bite (CRD re-apply, IAM, webhook, removed flags)?
   - **k8s_breaks** — removed/changed APIs in the jump window (`current < V <= target`).
4. Inspect namespace-scoped Pods with `app.kubernetes.io/instance=<release>`: phase outside Running/Succeeded **or** container READY `a/b` with `a != b` is unhealthy (including Running `2/3`). Missing container status counts as not ready.
5. Compare installed `helm get values <release> -n <ns> --all -o json` against chart-specific `risky_values`; exact typed matches WARN. Collection failure is `unknown` WARN; a missing path in computed values is INFO (경로 없음). Each injected release's `values` object must contain computed values including chart defaults, equivalent to `--all`.
6. Append findings to the audit log and exit with a gate code.

Use `--health-out before.json` before work and `--health-baseline before.json --health-out after.json` afterwards. Baseline suppresses only identical Pod name/phase/READY anomalies to INFO (기존 장애); new/changed anomalies WARN. Without baseline, anomalies WARN with existing/new `unknown`. Snapshots contain health only, never Helm values. Baselines must come from the same cluster; cluster identity is not verified.

Offline input: `--releases-json '[{"name":"ci","namespace":"build","chart":"jenkins-5.0.0","values":{"controller":{"installLatestPlugins":false}}}]' --pods-json '{"items":[...]}'`. `--pods-json` accepts a Kubernetes PodList, with metadata name/namespace/labels, spec containers and status phase/containerStatuses. With `--releases-json` and no `--pods-json`, no kubectl is called: INFO "health 미검사(pods 미주입)". Only live release listing enables live Pod reads. No matching Pods is INFO "검사 범위 밖(일치 Pod 없음)"; failed live Pod/values reads yield `unknown` + reason WARN. Only instance-labelled Pods are covered; operator-managed Pods without that label require separate inspection.

> If `helm` is unavailable or the cluster is unreachable, the script exits `127`. Do not silently
> treat this as a pass — report it and ask the user to fix access.

---

## Step 3: Interpret the Exit Code (do NOT override)

| Exit | Meaning | Action |
|---|---|---|
| `0` | OPEN — no compatibility problems | Proceed. |
| `1` | BLOCKED — CRITICAL finding(s) or output write failure | **Stop.** A chart does not support the target version (or hits a removed API). Resolve before upgrading. |
| `2` | WARN — HIGH finding(s) | Manual review required. Surface every finding to the user and get explicit confirmation. |
| `64` | Usage error — invalid CLI/JSON/baseline | **Not a gate verdict.** Fix the invocation and re-run. Never treat as WARN. |
| `127` | helm release listing unavailable / no cluster access | Report; ask user to install helm or fix kubeconfig. Not a pass. |

Every non-zero exit ends with `NEXT:`. Input errors stop with exit `64`; fix the invocation before re-running. **The exit code is authoritative.** Read it directly; never re-derive the verdict from the printed text.

---

## Step 4: Report to the User

Summarize the audit log findings in Korean. Group by severity and always include the remediation hint.

Example report shape:

```
## Helm 호환성 점검 결과 (1.33 → 1.34)

### ❌ 차단 (CRITICAL)
- ingress-nginx 4.13.5: K8s 1.33이 상한입니다. 1.34로 올리려면 차트를 4.14.x 이상으로 먼저 업그레이드하세요.

### ⚠️ 확인 필요 (HIGH)
- ingress-nginx: 프로젝트가 retirement 되었습니다 (2026-03 EOL). Gateway API 구현체로의 이전 계획이 필요합니다.

### ℹ️ 수동 검토 (INFO)
- my-internal-app 2.1.0: registry 미등록 차트입니다. kubeVersion·릴리스 노트를 직접 확인하세요.

감사 로그: <AUDIT_LOG_PATH>
```

For `unknown`-type charts (no machine-readable matrix), fetch the chart's `compat_source` URL to give the
user the latest official support guidance instead of guessing. The same applies whenever `HELM-STALE`
appears: the PASS stands, but say plainly that its basis is old and offer to re-verify.

---

## Known Limitations (state these honestly)

- **Registry coverage is curated, not exhaustive.** Only de-facto-standard charts are deeply modeled.
  Unregistered charts are flagged "manual review required" (INFO) — never a silent pass. The checker does
  not itself read `kubeVersion`; when reviewing an unregistered chart, inspect it manually with
  `helm show chart <release>` and consult the chart's release notes.
- **`kubeVersion` is a weak signal anyway.** Most charts declare only a lower bound (`>=1.21`), so it rarely
  catches incremental-upgrade blockers. The curated registry is the real source of truth.
- **Support matrices drift.** Registry data reflects the date it was curated, recorded per chart in
  `last_verified`. When a PASS rests on data older than 180 days the checker says so (`HELM-STALE`, INFO)
  rather than letting it pass silently — but it cannot know *what* changed. For `unknown` charts and
  borderline cases, the live `compat_source` fetch is authoritative.
- **`verified_k8s_max` is a human record, not a vendor guarantee.** For charts that publish no upper
  bound, it means "someone checked and found nothing blocking up to this version." Going past it
  re-raises the warning; it is never a permanent pass.
- This skill checks **chart compatibility**, not running-workload API usage. Pair it with EKS Insights /
  kubent (API scans) for full coverage — they are complementary, not redundant.

---

## Adding a Chart to the Registry

See `registry/_schema.md` for the full schema and checklist. In short: confirm the version scheme with
`helm show chart`, capture the official `compat_source` URL (do not rely on memory), classify the support
type, record lifecycle/EOL, extract upgrade hazards from release notes, and add an evaluation test.
