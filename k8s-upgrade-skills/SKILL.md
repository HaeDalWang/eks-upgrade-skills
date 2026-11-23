---
name: k8s-upgrade-skills
description: >
  Kubernetes version upgrade for Terraform-managed AWS EKS clusters.
  Discovers the Terraform/EKS layout, interviews once, plans, and enforces phase-gated safety.
  Note: disruption-aware (detects risks proactively) but does not guarantee zero-downtime.
  Trigger keywords: 'K8s upgrade', 'EKS upgrade', 'Kubernetes version upgrade', 'K8s version upgrade',
  'upgrade EKS', 'upgrade Kubernetes', 'cluster upgrade'
---

# Kubernetes Version Upgrade

This skill upgrades Terraform-managed AWS EKS clusters with workload protection.
It investigates the project and cluster read-only, runs the Phase 0 gate, asks the user once, writes `recipe.md`
and the upgrade plan, and executes [aws/terraform-eks/SKILL.md](aws/terraform-eks/SKILL.md) after approval.

All scripts referenced as `scripts/<name>.py` live in the **skill root directory** (this file's
directory). Resolve them relative to that root, not to the user's working directory.

---

## Step 1: Investigate, Interview, Plan

Order is fixed: discover → Phase 0 gate → one interview message → recipe → plan → approval.
Nothing changes infrastructure before `PLAN-APPROVED`.

### 1-A: Discover (read-only)

If `recipe.md` (preferred) or `recipe.yaml` exists in the working directory, validate it first
(`python3 scripts/validate_recipe.py recipe.md`; exit 1 → report and stop) and reuse its answers
as the previous answers: `target_version`, `tf_dir`, `auth_prefix`, `tf_var_file`, `services`,
`output_language`, notes. Old routing fields (`iac`, `platform`, `environment`) are ignored.

Always run discovery, also when a recipe exists (pass only the flags you have values for):

```bash
python3 scripts/discover.py --root . --target-version "${TARGET_VERSION}" \
  --tf-dir "${TF_DIR}" --auth-prefix "${AUTH_PREFIX}" --tf-var-file "${TF_VAR_FILE}" \
  --output discovery.json --audit-log audit.log
```

It finds the Terraform root calling `terraform-aws-modules/eks`, runs a lock-free trial
`terraform plan` + `show -json`, reads the cluster with aws/kubectl and the target AMI from
public SSM, and writes `discovery.json` (module address, cluster version variable and the file
that sets it, node pools, AMI variables with current/target values, service_watch candidates,
`unknowns`). Without `--target-version` the target is the live Control Plane minor + 1.

- exit 0 → everything settled.
- exit 2 `NEXT: ASK_USER <fields>` → put each listed field in the interview (1-C). For
  `auth_prefix` / `tf_var_file` / `terraform_init` / `tf_dir` / `cluster_name`, ask that one
  question alone, then rerun discovery with the answer before continuing.
- exit 1 `NEXT: STOP` → unsupported (no EKS module, plain `aws_eks_cluster`, several EKS
  modules in one root) or plan failure. Report and stop.
- exit 127 → missing CLI. Stop.

Never fill an `unknown` yourself. Read every name (`module.address`, `cluster_version.variable`,
`mng.ami_variable`, `karpenter.alias_variable`, ...) from `discovery.json`; do not assume names.

### 1-B: Phase 0 gate (before approval)

Run Phase 0 of [aws/terraform-eks/SKILL.md](aws/terraform-eks/SKILL.md) now, with
`CLUSTER_NAME`, `CURRENT_VERSION`, `TARGET_VERSION`, `TF_DIR` from `discovery.json`.
Exit 1 → show the Phase 0 remediation checklist and stop (no interview, no plan).
Exit 2 → the HIGH/MEDIUM findings go into the interview (1-C) for confirmation.

### 1-C: One interview message

Ask everything in **one** message. Use the tool's choice UI if it has one, recommended answer
first; otherwise numbered text with the recommended answer on each item. Ask only:

1. each discovery `unknown` (with its reason) and each Phase 0 WARN finding;
2. confirm `target_version` and the target AMI: `mng.target_release` and/or
   `karpenter.target_alias` (recommended: the discovered public SSM value);
3. confirm the service_watch candidates (`service_candidates`: name, namespace, via, ready pods,
   PDB, suggested `min_endpoints`) — recommended: watch all listed; the user may drop or add;
4. `output_language` only if no recipe sets it (recommended: `ko`).

With a reused recipe, ask only what differs from it. An answer that does not settle an item
(e.g. "알아서 해") takes the recommended answers you stated, and it is never plan approval.

### 1-D: Write recipe

Write the answers to `recipe.md` (overwrite: it is the answer record, not configuration) and
validate it with `python3 scripts/validate_recipe.py recipe.md`:

```markdown
---
cluster_name: <discovery cluster_name>
target_version: "<X.Y>"
output_language: <ko|en>
tf_dir: <discovery tf_dir>
auth_prefix: "<only if used>"
tf_var_file: <only if used>
services:
  - name: <svc>
    namespace: <ns>
    min_endpoints: <n>
---

## 업그레이드 컨텍스트

<user notes; omit when none>
```

| Field | Required | Purpose |
|---|---|---|
| `cluster_name` | yes | Target for aws eks / kubectl commands |
| `target_version` | yes | `"X.Y"`; only current minor + 1 (no skip, no downgrade) |
| `output_language` | no (`ko`) | Language of user-facing output |
| `tf_dir` | no | Confirmed Terraform root (discovery `tf_dir`) |
| `auth_prefix` | no | Prefix for every `terraform`/`aws` call, e.g. `aws-runas my-profile`; pass as `--auth-prefix` to gates |
| `tf_var_file` | no | `-var-file` for terraform plans; pass as `--tf-var-file` to gates |
| `services` | no | service_watch targets: `name`, `namespace`, `min_endpoints`, optional `health_check_url`. Absent → service_watch skipped |
| `notes` | no | `recipe.yaml` only; in `recipe.md` use the body |

### 1-E: Generate the plan

```bash
python3 scripts/upgrade_plan.py create --discovery discovery.json \
  --output-language "${OUTPUT_LANGUAGE}" --plan-doc upgrade-plan.json
```

Node pools, per-step targets and AMI expectations come from `discovery.json`. Explicit flags
override it: `--target STEP=ADDRESS` / `--expect STEP:NAME=VALUE` replace that step's
discovered values (use them for AMI values the user gave in the interview), `--node-pool`
replaces the pools. Exit 2 `NEXT: ASK_USER` → a node step still lacks its AMI variable/value;
ask, then rerun create with `--expect`.
The default apply order is cp → nodes (MNG only) → karpenter (Karpenter only) → full.
A custom `--order cp,addon,nodes,karpenter,full` requires `--reason "..."`; deviations appear in the rendering.
`upgrade-plan.json` is authoritative; display the generated `upgrade-plan-<cluster>-<YYYYMMDD>.md`
in full, together with the Phase 0 result, recipe context and service-monitoring details.
Existing output files are never overwritten by create. For a revised plan, choose a new `--plan-doc` path
in a separate output directory, display it and request approval again.

### 1-F: Wait for Approval

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

## Step 2: Execute

Read [aws/terraform-eks/SKILL.md](aws/terraform-eks/SKILL.md) and follow its phases sequentially from Phase 0 onward
(Phase 0 already ran in Step 1-B; its section says when to rerun it). Every phase gate still runs.

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
