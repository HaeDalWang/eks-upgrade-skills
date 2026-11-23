# Phase 3: add-on safety gate

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Confirm the step id. In the default plan there is no `addon` step: the current step is the next one
(`nodes`, `karpenter` or `full`) and you run only 3-1 and 3-2 here.

**Custom plans with an `addon` step** (current `type` is `addon`): before 3-1, run the cp.md
2-1 → 2-3 sequence with planfile `"${TF_DIR}/addon.tfplan"` and this step's own targets instead of
`${EKS_MODULE}` (plan → `plan_check.py` → `apply_guard.py` → apply, each only after exit 0), then
finish it after the 3-2 PASS with `upgrade_plan.py done --step-id <its id>`.

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

(No `--target-version`: phase3 rejects it.)

**WARN (exit 2), add-ons still `UPDATING`**: add-ons reconcile one after another (commonly vpc-cni →
coredns → kube-proxy), so `UPDATING` can repeat two or three times. This is normal; do not ask the user
to rerun:

1. wait 30 s, then rerun the Phase 3 gate;
2. repeat up to about 6 times (about 3 minutes) until exit 0;
3. STOP only on a terminal bad state (`DEGRADED` / `*_FAILED`, reported as exit 1).

New Fargate pods may be `Pending` briefly while a Fargate node provisions (an
`aws-logging configmap not found` warning is normal without Fargate logging). They clear once the add-on
is `ACTIVE`.

On PASS open [nodes.md](nodes.md).
