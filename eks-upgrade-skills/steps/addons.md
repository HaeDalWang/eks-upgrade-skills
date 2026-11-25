# Phase 3: add-on safety gate

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Current `type` is `addon` (custom plans only) → run the whole file with its id (default `addon`).
Otherwise this is the gate-only run (SKILL.md): skip 3-0, run 3-1 and **3-2, which always runs**, and
do not run `done`.

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

## 3-0. Custom `addon` step: monitor, plan, guard and apply

Set `ADDON_TARGET` to the step's targets. Start the drain monitor with **`run_in_background: true`**
and wait for `DRAIN-P2 | START` (cp.md stopped its own), then run each command only after exit 0
(409/timeouts: cp.md 2-3-a / 2-3-b):

```bash
python3 scripts/drain_watch.py --phase P2 --scope kube-system --audit-log audit.log
```

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -target="${ADDON_TARGET}" -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/addon.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/addon.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Call 1: run apply_guard in a separate foreground Bash call, only after the preceding command exits 0.

```bash
python3 scripts/apply_guard.py --planfile "${TF_DIR}/addon.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
User runs this apply → follow [user-apply.md](user-apply.md) here, then return to this step.
Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
The hook denies `$`/subshells in an apply command.

```bash
cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/addon.tfplan
```

## 3-1. Wait for add-on stabilization

Add-ons reconcile after the Control Plane upgrade. Wait up to 5 minutes, polling every 30 s:

```bash
${AUTH_PREFIX} aws eks list-addons --cluster-name "${CLUSTER_NAME}" --query 'addons[]' --output text \
  | tr '\t' '\n' | while read addon; do
    ${AUTH_PREFIX} aws eks describe-addon --cluster-name "${CLUSTER_NAME}" --addon-name "$addon" \
      --query '{name:addon.addonName, version:addon.addonVersion, status:addon.status}' --output json
  done
```

## 3-2. Phase 3 gate (always)

```bash
python3 scripts/phase_gate.py phase3 \
  --cluster-name "${CLUSTER_NAME}" \
  --audit-log audit.log
```

`CONFLICT` FAIL: follow the PRESERVE guidance in `NEXT`, STOP and write the failure report; only the operator changes Terraform.

**WARN (exit 2), add-ons still `UPDATING`**: add-ons reconcile one after another (commonly vpc-cni →
coredns → kube-proxy), so `UPDATING` can repeat two or three times. This is normal; do not ask the user
to rerun:

1. wait 30 s, then rerun the Phase 3 gate;
2. repeat up to about 6 times (about 3 minutes) until exit 0;
3. STOP only on a terminal bad state (`DEGRADED` / `*_FAILED`, reported as exit 1).

New Fargate pods may be `Pending` briefly while a Fargate node provisions (an
`aws-logging configmap not found` warning is normal without Fargate logging). They clear once the add-on
is `ACTIVE`.

On PASS, finish the step only if the current type is `addon`, then stop the P2 monitor you started in 3-0:

```bash
python3 scripts/upgrade_plan.py done --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id addon
```

Return to the SKILL.md step loop.
