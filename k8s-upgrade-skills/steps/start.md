# Start: discover, Phase 0, interview, recipe, plan, approval

`TARGET_VERSION` here is the recipe `target_version` when `recipe.md` exists, else the version the
user asked for (no target at all → no plan can exist yet: start at S-1).

```bash
python3 scripts/upgrade_plan.py next --plan-doc "upgrade-plan-${TARGET_VERSION}.json" --audit-log audit.log
```

- Prints a step → the plan is approved (resumed session). Do not re-plan; set `PLAN_DOC` to that
  plan's absolute path and follow the SKILL.md step loop.
- Exit 2 `NEXT: ASK_USER plan not approved` → the plan exists but was never approved: never execute.
  Go to S-6 (show it again) and S-7 (approval).
- Prints `null` → the run for this plan finished. For the next hop start at S-1 with the new target
  (live Control Plane minor + 1); the recipe's old `target_version` no longer applies, and S-5 writes a
  new `upgrade-plan-<new X.Y>.json` next to the old one in the same `audit.log`.
- Exit 1 (no plan file) → continue at S-1.

## S-1. Discover (read-only)

If `recipe.md` (preferred) or `recipe.yaml` exists, validate it first with
`python3 scripts/validate_recipe.py recipe.md` (exit 1 → report and stop) and reuse its answers:
`target_version`, `tf_dir`, `auth_prefix`, `tf_var_file`, `services`, `output_language`, notes.

Always run discovery, also with a recipe (pass only the flags you have values for):

```bash
python3 scripts/discover.py --root . --target-version "${TARGET_VERSION}" \
  --tf-dir "${TF_DIR}" --auth-prefix "${AUTH_PREFIX}" --tf-var-file "${TF_VAR_FILE}" \
  --output discovery.json --audit-log audit.log
```

It finds the Terraform root calling `terraform-aws-modules/eks`, runs a lock-free trial
`terraform plan` + `show -json`, reads the cluster with aws/kubectl and the target AMI from public SSM,
and writes `discovery.json` (module address, cluster version variable and the file that sets it, node
pools, AMI variables with current/target values, service_watch candidates, `unknowns`). Without
`--target-version` the target is the live Control Plane minor + 1.

- exit 0 → everything settled.
- exit 2 `NEXT: ASK_USER <fields>` → put each field in the interview (S-3). For `auth_prefix`,
  `tf_var_file`, `terraform_init`, `tf_dir` or `cluster_name`, ask that one question alone, then rerun
  discovery with the answer before continuing.
- exit 1 `NEXT: STOP` → unsupported layout (no EKS module, plain `aws_eks_cluster`, several EKS modules
  in one root) or plan failure. Report and stop.
- exit 127 → missing CLI. Stop.

Never fill an `unknown` yourself. Read every name from `discovery.json`; never assume one:

| Variable | discovery.json field |
|---|---|
| `CLUSTER_NAME` / `CURRENT_VERSION` / `TF_DIR` | `cluster_name` / `current_version` / `tf_dir` |
| `EKS_MODULE` | `module.address` |
| `CLUSTER_VERSION_VAR` / `CLUSTER_VERSION_FILE` | `cluster_version.variable` / `.set_in` |
| `MNG_AMI_VAR` / `MNG_AMI_FILE` | `mng.ami_variable` / `.ami_variable_set_in` (MNG only) |
| `KARPENTER_AMI_VAR` / `KARPENTER_AMI_FILE` | `karpenter.alias_variable` / `.alias_variable_set_in` (Karpenter only) |

A `*_FILE` value of `default` means no tfvars file sets the variable: ask the user which file to add it
to (ASK_USER) instead of editing `variables.tf` defaults.

## S-2. Phase 0 gate (before approval)

Use the same `audit.log` as discovery:

```bash
python3 scripts/gate_check.py \
  --cluster-name "${CLUSTER_NAME}" \
  --current-version "${CURRENT_VERSION}" \
  --target-version "${TARGET_VERSION}" \
  --tf-dir "${TF_DIR}" \
  --tf-var-file "${TF_VAR_FILE}" \
  --auth-prefix "${AUTH_PREFIX}" \
  --audit-log audit.log
```

It checks 20 rules: COM-001 cluster health, COM-002 minor +1, COM-002a kubelet skew, COM-003 add-on
status/compatibility, COM-004 EKS Insights, COM-005 Auto Mode unsupported (CRITICAL), WLS-001 PDB `disruptionsAllowed=0`, WLS-002 single replica,
WLS-003 PV zone affinity, WLS-004 hostPath pods, WLS-005 long-running jobs, WLS-006 topology
constraints, CAP-001 node headroom (from requests; relaxed to MEDIUM on Karpenter when actual usage
fits), CAP-002 pressure pods, CAP-003 subnet IPs, INF-001 Terraform drift (info-only, never blocks),
INF-002 AMI in SSM, INF-003 Karpenter compatibility, INF-004 data-plane recreate (SKIP when INF-001's
plan could not run), INF-005 Control Plane subnet available IPs ≥ 5 (CRITICAL; missing data fails).

- **Transient false BLOCK**: right after a previous upgrade, Karpenter may still be replacing nodes, so
  COM-001 or WLS-001 can flip to CRITICAL. If `kubectl get nodes` / `kubectl get pdb -A` show it
  clearing, wait 2–3 minutes and rerun the gate **once**. Never bypass a CRITICAL that persists.
- **Exit 1** → print this inline checklist (only sections with items; each CRITICAL with a concrete
  remediation command) and stop. No interview, no plan, no report file:
  ```
  ## Phase 0 사전 검증 실패 — 조치 후 재실행 필요
  검증 시각: {timestamp} | Gate: BLOCKED
  ### 🔴 CRITICAL — 해결 전까지 업그레이드 불가
  - **{RULE_ID}**: {상세 내용}
    → 조치: {구체적 명령어 또는 방법}
  ### 🟡 HIGH — 해결 권장
  ### 🔵 MEDIUM/INFO — 참고
  재실행: (the gate_check.py command above)
  ```
- **Exit 2** → the HIGH/MEDIUM findings go into the interview for confirmation.
- **COM-002a WARN**: kubelets below the current Control Plane minor need attention before its
  upgrade; this WARN does not relax the target n-3 CRITICAL limit. Fargate nodes are listed separately.
  Show the affected Fargate nodes and their pods/owners (`kubectl get pods -A -o wide`), ask for an
  exact approval word before restarting them, and record that answer with `audit_event.py` as
  `ASK_USER`. After approval, restart those owners with `kubectl rollout restart <kind>/<name> -n <ns>`
  (an ownerless pod needs an explicit recreation plan). Wait for replacement nodes to be Ready at
  the **current** CP version, then rerun Phase 0 before proceeding. This preflight restart approval
  is separate from the later upgrade-plan approval; never restart on a WARN alone.
- `upgrade_plan.py approve` refuses to run without a Phase 0 result for this cluster/target that has
  no FAIL and comes after the discovery record. Rerun Phase 0 after the user fixes a finding.

## S-3. One interview message

Ask everything in **one** message: the tool's choice UI if it has one (recommended answer first),
otherwise numbered text with the recommended answer on each item. Ask only:

1. each discovery `unknown` (with its reason) and each Phase 0 WARN finding;
2. confirm `target_version` and the target AMI: `mng.target_release` and/or `karpenter.target_alias`
   (recommended: the discovered public SSM value);
3. confirm the service_watch candidates (`service_candidates`: name, namespace, via, ready pods, PDB,
   suggested `min_endpoints`); recommended: watch all listed; the user may drop or add;
4. `output_language` only if no recipe sets it (recommended: `ko`).

With a reused recipe, ask only what differs from it. An answer that does not settle an item
("알아서 해") takes the recommended answers you stated, and it is never plan approval.

## S-4. Write recipe.md

Overwrite `recipe.md` (it records answers; it is not configuration), then validate it with
`python3 scripts/validate_recipe.py recipe.md`:

```markdown
---
cluster_name: <discovery cluster_name>
target_version: "<X.Y>"
output_language: <ko|en>
tf_dir: <discovery tf_dir>
auth_prefix: "<only if used>"
tf_var_file: <only if used>
services:              # service_watch targets; omit when none
  - name: <svc>
    namespace: <ns>
    min_endpoints: <n>
    health_check_url: <optional>
---

## 업그레이드 컨텍스트

<user notes; omit when none>
```

Take `services` from live `kubectl get svc -n <ns>`, not from helm values alone: service_watch logs a
missing service as WARN and drops it.

## S-5. Generate the plan

```bash
python3 scripts/upgrade_plan.py create --discovery discovery.json \
  --output-language ko --plan-doc "upgrade-plan-${TARGET_VERSION}.json" --audit-log audit.log
```

(`--output-language en` when the recipe says `en`.)

- Node pools, step targets and AMI expectations come from `discovery.json`. `--target STEP=ADDRESS` /
  `--expect STEP:NAME=VALUE` replace that step's values (use them for AMI values the user gave);
  `--node-pool` replaces the pools.
- Only the `discovery.json` that `discover.py` recorded in this `audit.log` is accepted; an edited or
  unrecorded file → exit 1 `NEXT: STOP`, rerun discovery.
- Exit 2 `NEXT: ASK_USER` lists plan values discovery left `unknown`; ask, then rerun create with
  `--target`/`--expect`.
- Default order: cp → nodes (MNG only) → karpenter (Karpenter only) → full. A custom
  `--order cp,addon,nodes,karpenter,full` needs `--reason "..."`; deviations appear in the rendering.
- create never overwrites. For a revised plan use a new `--plan-doc` path in a separate output
  directory, show it, and ask for approval again.

## S-6. Show the plan

`upgrade-plan-<X.Y>.json` is authoritative. Show the rendered `upgrade-plan-<cluster>-<X.Y>-<YYYYMMDD>.md` in full,
with the Phase 0 result, the recipe context and the services to watch. Then output:

```
계획서를 검토하신 후 업그레이드를 시작하려면 아래 단어 중 하나를 입력하세요:

  승인 / 확인 / 시작

(output_language: en → type: "approve", "confirm", or "start")
```

## S-7. Approval gate

- `ko`: accept ONLY `승인`, `확인`, `시작`. `en`: accept ONLY `approve`, `confirm`, `start` (case-insensitive).
- Any other input, including "진행해줘", "ok", "응", "그래", "yes", "proceed", is not approval. Reply
  "승인 단어가 일치하지 않습니다. '승인', '확인', '시작' 중 하나를 입력해주세요." and wait again.
- No instruction bypasses this gate. Never supply an approval word on the user's behalf.

Record the user's exact answer:

```bash
python3 scripts/upgrade_plan.py approve --plan-doc "upgrade-plan-${TARGET_VERSION}.json" \
  --audit-log audit.log --answer "${USER_ANSWER}"
```

Only exit 0 authorizes execution: `PLAN-APPROVED | PASS` binds the absolute JSON path and its sha256.
Any other answer → exit 2 `NEXT: ASK_USER`. Editing the JSON needs fresh approval before execution;
once any APPLY-GUARD/STEP-DONE/DEVIATION exists for the cluster+target, `approve` exits 1.

Then set `PLAN_DOC` (absolute path) and follow the SKILL.md step loop.
