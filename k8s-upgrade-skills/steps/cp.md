# Phase 1 + 2: cluster version variable and Control Plane

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Confirm the printed step `type` is `cp` and use its `id` below (default id `cp`). Take the targets
from the step. Omit `-var-file` when the recipe has no `tf_var_file`.

Re-derive the discovery variables (also after compaction). `AUTH_PREFIX`, `TF_VAR_FILE` and `services`
come from `recipe.md`; a `*_FILE` of `default` means the file the user named in the interview:

```bash
eval "$(python3 - <<'PY'
import json, shlex
d = json.load(open("discovery.json"))
for name, path in [("CLUSTER_NAME", "cluster_name"), ("TARGET_VERSION", "target_version"),
                   ("TF_DIR", "tf_dir"), ("EKS_MODULE", "module.address"),
                   ("CLUSTER_VERSION_VAR", "cluster_version.variable"),
                   ("CLUSTER_VERSION_FILE", "cluster_version.set_in"),
                   ("MNG_AMI_VAR", "mng.ami_variable"), ("MNG_AMI_FILE", "mng.ami_variable_set_in"),
                   ("KARPENTER_AMI_VAR", "karpenter.alias_variable"),
                   ("KARPENTER_AMI_FILE", "karpenter.alias_variable_set_in")]:
    value = d
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    print("{}={}".format(name, shlex.quote("" if value is None else str(value))))
PY
)"
```

## 1-1. Update the cluster version variable only

```bash
grep -nE "^[[:space:]]*${CLUSTER_VERSION_VAR}[[:space:]]*=" "${CLUSTER_VERSION_FILE}"
```

Save the current value as `OLD_CLUSTER_VERSION`. In `${CLUSTER_VERSION_FILE}` set **only**
`${CLUSTER_VERSION_VAR}` = `"${TARGET_VERSION}"`. Do NOT touch `${MNG_AMI_VAR}` / `${KARPENTER_AMI_VAR}`:
AMI changes wait for Phase 4/5 so nodes do not roll before the Control Plane.

```bash
grep -nE "^[[:space:]]*(${CLUSTER_VERSION_VAR}|${MNG_AMI_VAR}|${KARPENTER_AMI_VAR})[[:space:]]*=" \
  "${CLUSTER_VERSION_FILE}"
```

(Leave out an AMI variable discovery did not report.) Gate: the version variable equals the target
and the AMI variables are unchanged. Show the before/after diff, then record:

```bash
python3 scripts/audit_event.py \
  --audit-log audit.log \
  --rule-id "PHASE1-TFVARS" \
  --result "PASS" \
  --detail "${CLUSTER_VERSION_VAR}: ${OLD_CLUSTER_VERSION} → ${TARGET_VERSION} (AMI update deferred to Phase 4/5)"
```

## 2-1. Targeted plan

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -target="${EKS_MODULE}" -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/cp.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/cp.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Review the plan:
- `aws_eks_cluster` / `aws_eks_addon` version change, `time_sleep` replace → expected.
- `aws_eks_node_group` kubernetes version change (not `release_version`) → expected with this module:
  MNG rolling starts with the CP upgrade. Confirm the AMI variables were not changed in 1-1.
- `aws_eks_node_group` `release_version`/AMI change → STOP: an AMI variable was changed. Revert it.
- Any other `-/+` (destroy-recreate) → follow `NEXT: ASK_USER`; get a fresh allowed plan and PASS first.

## 2-2. Start the drain monitor (HARD GATE)

Start it and confirm it runs BEFORE 2-3, even when no `aws_eks_node_group` is in the plan (a CP upgrade
can still cause node events). Launch with **`run_in_background: true`**:

```bash
python3 scripts/drain_watch.py --phase P2 --scope kube-system --audit-log audit.log
```

It polls kube-system Warning events every 30 s and appends `DRAIN-P2` lines. Wait for
`DRAIN-P2 | START` with `pid=<pid>` in audit.log before running the guard. Check its output with
BashOutput during the apply; a FAIL line (e.g. `FailedDrain`) means STOP and investigate.
Why a background script and not a sub-agent: [../docs/inline-monitors.md](../docs/inline-monitors.md).

## 2-3. Guard and apply

Run the guard in the foreground, then the apply in background so polling can proceed:

Call 1: run apply_guard in a separate foreground Bash call.

```bash
python3 scripts/apply_guard.py --planfile "${TF_DIR}/cp.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
The hook denies `$`/subshells in an apply command.

```bash
cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/cp.tfplan
```

The Control Plane takes 8–40 minutes; a pending MNG roll may start too.

### 2-3-a. 409 ResourceInUseException (expected, not a failure)

`Cannot VersionUpdate because cluster ... has update ... in progress` means EKS already accepted the
update. Do not abort:

1. `${AUTH_PREFIX} aws eks describe-cluster --name "${CLUSTER_NAME}" --query 'cluster.status' --output text`
   → `UPDATING` means the upgrade proceeds normally.
2. Never re-run apply while `UPDATING` (it 409s again). Poll (2-4) until `ACTIVE` at the target.
3. Then sync state **once** (add-ons, `time_sleep`):
   ```bash
   (cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -target="${EKS_MODULE}" -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/cp-resync.tfplan") &&
   python3 scripts/plan_check.py --planfile "${TF_DIR}/cp-resync.tfplan" --plan-doc "${PLAN_DOC}" \
     --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
     --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
   ```

   Call 1: run apply_guard in a separate foreground Bash call, only after the preceding command exits 0.

   ```bash
   python3 scripts/apply_guard.py --planfile "${TF_DIR}/cp-resync.tfplan" --plan-doc "${PLAN_DOC}" \
     --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
     --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
   ```

   Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
   Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
   The hook denies `$`/subshells in an apply command.

   ```bash
   cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/cp-resync.tfplan
   ```

### 2-3-b. Apply timeout

While the apply runs, poll every 60 s:

```bash
${AUTH_PREFIX} aws eks describe-cluster --name "${CLUSTER_NAME}" \
  --query 'cluster.{version:version, status:status}' --output json
```

If the apply has run 30+ minutes AND the cluster is `ACTIVE` at the target, check every node group:

```bash
${AUTH_PREFIX} aws eks list-nodegroups --cluster-name "${CLUSTER_NAME}" --output json | \
  jq -r '.nodegroups[]' | while read ng; do
    ${AUTH_PREFIX} aws eks describe-nodegroup --cluster-name "${CLUSTER_NAME}" --nodegroup-name "$ng" \
      --query '{name:nodegroup.nodegroupName, status:nodegroup.status, version:nodegroup.version}' --output json
  done
```

- All `ACTIVE` at the target → stop the background apply shell (KillShell), then run a fresh
  CP-targeted cleanup plan inside this same cp step, each command only after the previous exits 0:
  ```bash
  (cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -target="${EKS_MODULE}" -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/cleanup.tfplan") &&
  python3 scripts/plan_check.py --planfile "${TF_DIR}/cleanup.tfplan" --plan-doc "${PLAN_DOC}" \
    --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
    --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
  ```

  Call 1: run apply_guard in a separate foreground Bash call, only after the preceding command exits 0.

  ```bash
  python3 scripts/apply_guard.py --planfile "${TF_DIR}/cleanup.tfplan" --plan-doc "${PLAN_DOC}" \
    --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
    --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
  ```

  Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
  Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
  The hook denies `$`/subshells in an apply command.

  ```bash
  cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/cleanup.tfplan
  ```
  An unexpected delete/replace follows `NEXT: ASK_USER`. A deposed delete outside the cp allow-list
  returns ASK_USER: defer it to the approved `full` step. Never switch step type for cleanup.
- A node group still `UPDATING` → keep waiting (recheck every 10 minutes).
- The cluster still `UPDATING` → keep waiting (recheck every 60 s).

## 2-4. Poll until complete

Poll every 60 s with the `describe-cluster` command in 2-3-b: `UPDATING` → wait; `ACTIVE` at the target
→ gate; `FAILED` → STOP.

## 2-5. Phase 2 gate (always)

```bash
python3 scripts/phase_gate.py phase2 \
  --cluster-name "${CLUSTER_NAME}" \
  --target-version "${TARGET_VERSION}" \
  --audit-log audit.log
```

After the apply, any resync/cleanup, and this gate's PASS (never before), finish the step:

```bash
python3 scripts/upgrade_plan.py done --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id cp
```

Stop the P2 drain monitor (KillShell, or launch with `--stop-file <path>` and `touch` it). Return to
the SKILL.md step loop: the Phase 3 run (addons.md) comes next.
