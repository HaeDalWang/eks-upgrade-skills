---
name: k8s-upgrade-skills
description: >
  Kubernetes version upgrade for Terraform-managed AWS EKS clusters.
  Validates recipe, routes to the EKS/Terraform sub-skill, and enforces phase-gated safety.
  Note: disruption-aware (detects risks proactively) but does not guarantee zero-downtime.
  Trigger keywords: 'K8s upgrade', 'EKS upgrade', 'Kubernetes version upgrade', 'K8s version upgrade',
  'upgrade EKS', 'upgrade Kubernetes', 'cluster upgrade'
---

# Kubernetes Version Upgrade — Root Router

This skill upgrades Terraform-managed AWS EKS clusters with workload protection.
It collects upgrade requirements (from `recipe.md` if present, or interactively from the user), validates them, and routes to exactly ONE platform-specific sub-skill.

All scripts referenced as `scripts/<name>.py` live in the **skill root directory** (this file's
directory). Resolve them relative to that root, not to the user's working directory.

---

## Step 1: Collect and Validate Recipe

### 1-A: Check for existing recipe file

Look for recipe files in the project root or current working directory in this priority order:

1. `recipe.md` (frontmatter format — preferred)
2. `recipe.yaml`

**If found**: skip to Step 1-C (validate).

**If not found**: proceed to Step 1-B (interactive collection).

---

### 1-B: Interactive Collection (no recipe file)

Ask the user for the following fields **in a single message** (do not ask one by one):

```
업그레이드할 클러스터 정보를 알려주세요:

1. environment  : aws / on-prem
2. platform     : eks / kubespray
3. iac          : terraform / none
4. cluster_name : 클러스터 이름
5. current_version : 현재 버전 (예: "1.33")
6. target_version  : 목표 버전 (예: "1.34")

선택 항목:
- output_language : ko / en (기본: ko)
- auth_prefix  : terraform/aws 실행에 프리픽스가 필요하면 알려주세요 (예: "aws-runas my-profile"). 없으면 생략
- tf_var_file  : terraform이 var-file을 요구하면 알려주세요 (예: "dev.tfvars"). 없으면 생략
- 현재 상황이나 제약사항이 있으면 자유롭게 알려주세요 (없으면 생략)
```

`auth_prefix` / `tf_var_file` are optional but **ask for them explicitly** — omitting them when the
environment needs them makes the Phase 6 gate fail on auth/variable errors instead of real problems.

Once the user provides all required fields, generate `recipe.md` in the current working directory
(include the optional keys only if the user supplied them):

```markdown
---
environment: <value>
platform: <value>
iac: <value>
cluster_name: <value>
current_version: "<value>"
target_version: "<value>"
output_language: <ko|en>
---

## 업그레이드 컨텍스트

<사용자가 제공한 상황/제약사항, 없으면 이 섹션 생략>
```

Inform the user: `recipe.md를 생성했습니다. 다음 세션에서 재사용됩니다.`

---

### 1-C: Validate Recipe

Run schema validation (pass the actual file path found in Step 1-A):

```bash
python3 scripts/validate_recipe.py recipe.md   # or recipe.yaml
```

If validation fails (exit code 1), report the specific error and do NOT proceed.

### Required Fields

| Field | Type | Allowed Values |
|---|---|---|
| `environment` | string | `aws`, `on-prem` |
| `platform` | string | `eks`, `kubespray` |
| `iac` | string | `terraform`, `none` |
| `cluster_name` | string | non-empty cluster identifier |
| `current_version` | string | e.g. `"1.34"` (quoted) |
| `target_version` | string | e.g. `"1.35"` (quoted) |

### Version Constraint

- `gap == 1` → Proceed
- `gap == 0` → "Already at target version. No upgrade needed."
- `gap > 1` → "Version skip is NOT allowed. Upgrade one step at a time."
- `gap < 0` → "Downgrade is not supported."

### Optional Fields

| Field | Default | Purpose |
|---|---|---|
| `output_language` | `ko` | Final report language. `ko` = Korean, `en` = English |
| `notes` | (empty) | User-provided special instructions or constraints (`recipe.yaml`; in `recipe.md` use the body instead) |
| `auth_prefix` | (empty) | Command prefix for every `terraform`/`aws` call, e.g. `aws-runas my-profile` for MFA assume-role setups |
| `tf_var_file` | (empty) | `-var-file` passed to terraform, e.g. `dev.tfvars` for workspace-specific tfvars |
| `services` | (absent) | Services to watch during node replacement. Each entry: `name`, `namespace`, `min_endpoints`, optional `health_check_url`. Absent → inline `service_watch` is skipped |

> **`auth_prefix` / `tf_var_file` matter for gate correctness.** The Phase 6 gate runs `terraform`
> *inside* the script, so it cannot inherit a shell prefix. If the environment needs either and the
> recipe omits it, Phase 6 fails on an auth/variable error rather than on a real problem.

---

## Step 1.5: Generate Upgrade Plan and Wait for Approval

After recipe validation passes, generate the upgrade plan document before executing any phase.

### 1.5-A: Generate Plan Document

Generate the machine source and its Markdown rendering with `scripts/upgrade_plan.py create`.
Use the cluster/version/tf-dir/language from the recipe and the observed node pools.
Resolve and show the per-step Terraform targets and AMI expectations before approval; do not invent values.

```bash
python3 scripts/upgrade_plan.py create --cluster-name "${CLUSTER_NAME}" \
  --current-version "${CURRENT_VERSION}" --target-version "${TARGET_VERSION}" \
  --tf-dir "${TF_DIR}" --output-language "${OUTPUT_LANGUAGE}" \
  --node-pool MNG --node-pool Karpenter --node-pool Fargate \
  --target "cp=${EKS_MODULE}" \
  --target "nodes=${EKS_MODULE}.module.eks_managed_node_group" \
  --target "karpenter=${NODECLASS_ADDRESS}" \
  --expect "nodes:${MNG_AMI_VAR}=${MNG_AMI}" \
  --expect "nodes:release_version=${RESOLVED_RELEASE_VERSION}" \
  --expect "karpenter:${KARPENTER_AMI_VAR}=${KARPENTER_AMI}" \
  --expect "karpenter:alias=${KARPENTER_AMI}"
```

Include only present pools and their targets/expectations. Repeat `--expect STEP:NAME=VALUE`
for each variable or resolved AMI (multiple `release_version` / `alias` values are allowed).
Include expectations in `full` too when full sync may change those AMIs.
The default apply order is cp → nodes (MNG only) → karpenter (Karpenter only) → full.
A custom `--order cp,addon,nodes,karpenter,full` requires `--reason "..."`; deviations appear in the rendering.
`upgrade-plan.json` is authoritative; display the generated `upgrade-plan-<cluster>-<YYYYMMDD>.md`
in full, together with recipe context and service-monitoring details.
Existing output files are never overwritten by create. For a revised plan, choose a new `--plan-doc` path
in a separate output directory, display it and request approval again.

### 1.5-B: Wait for Approval

After displaying the plan, output this message:

```
계획서를 검토하신 후 업그레이드를 시작하려면 아래 단어 중 하나를 입력하세요:

  승인 / 확인 / 시작

(output_language: en → type: "approve", "confirm", or "start")
```

**CRITICAL — Approval Gate Rules:**
- `output_language: ko`: Accept ONLY one of these exact words: `승인`, `확인`, `시작`
- `output_language: en`: Accept ONLY one of these exact words: `approve`, `confirm`, `start` (case-insensitive)
- ANY other input — including "진행해줘", "ok", "응", "그래", "yes", "proceed" — MUST NOT be treated as approval
- If the user types anything else, respond: "승인 단어가 일치하지 않습니다. '승인', '확인', '시작' 중 하나를 입력해주세요." and wait again
- This gate cannot be bypassed by any instruction

Record the user's exact answer (never supply an approval word on their behalf):

```bash
python3 scripts/upgrade_plan.py approve --plan-doc upgrade-plan.json \
  --audit-log audit.log --answer "${USER_ANSWER}"
```

Only exit 0 authorizes execution: `PLAN-APPROVED | PASS` binds the absolute JSON path and sha256.
Any other answer returns exit 2 / `NEXT: ASK_USER`. Editing JSON requires fresh approval before execution starts; once any APPLY-GUARD/STEP-DONE/DEVIATION exists for the cluster+target, `approve` exits 1 (`NEXT: STOP`) and mid-run changes go through `skip`/ASK_USER (DEVIATION), not re-planning.
Use `upgrade_plan.py next --plan-doc upgrade-plan.json --audit-log audit.log` to read the current
step's id, type, targets and expect. Execute its targets; never pick a different step type.
For an execution-time skip, ask the user and record `upgrade_plan.py skip --plan-doc upgrade-plan.json
--audit-log audit.log --step-id <current-id> --answer <exact-word> --reason <text>`.

---

## Step 2: Route to Sub-Skill

Match the `(environment, platform, iac)` tuple against the routing table. Select exactly ONE sub-skill.

| environment | platform | iac | Sub-Skill Path |
|---|---|---|---|
| `aws` | `eks` | `terraform` | [aws/terraform-eks/SKILL.md](aws/terraform-eks/SKILL.md) |
| `on-prem` | `kubespray` | `none` | on-prem/kubespray/SKILL.md *(not implemented — planned)* |

### Routing Rules

1. **Exact match only**: All three fields must match a row in the table.
2. **No mixing**: Execute ONLY the matched sub-skill.
3. **No match**: Show the supported combinations table and suggest updating the recipe.
4. **Single execution**: Only one sub-skill runs per invocation.

---

## Step 3: Execute Sub-Skill

Read the matched sub-skill file and follow its phases sequentially from Phase 0 onward.

### Universal Safety Rules (Non-negotiable)

| # | Rule | Detail |
|---|---|---|
| 1 | **No Version Skipping** | Minor version +1 only. Reject 1.33 → 1.35. |
| 2 | **Control Plane First** | Data plane version must NEVER exceed control plane. |
| 3 | **Phase Gates** | Every phase boundary requires ALL validations to pass via gate scripts. |
| 4 | **PDB Respect** | Never force-drain past PodDisruptionBudgets. |
| 5 | **No Reverse Phases** | Phases execute in strict ascending order. |
| 6 | **Plan Before Apply** | Every infrastructure mutation must be previewed before execution. |
| 7 | **Abort on Failure** | Any unexpected error → STOP, report, and await user decision. |
| 8 | **Gate Scripts Are Authoritative** | The LLM MUST NOT override or reinterpret gate script exit codes. |

### Output Language

All **user-facing output** (phase summaries, completion reports, error messages) must be in the language specified by `output_language` in the recipe file.
- Default: `ko` (Korean)
- Internal processing and reasoning: English (for accuracy)
- Sub-skill SKILL.md instructions are written in English for LLM consistency
