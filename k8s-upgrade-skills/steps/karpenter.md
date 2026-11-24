# Phase 5: Karpenter nodes

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Confirm the step id. Current `type` is `karpenter` → run the whole file with its id (default
`karpenter`). Otherwise this is the gate-only run (SKILL.md, no Karpenter in the plan): skip 5-0 → 5-1
and do not run `done`, but **5-2 always runs**: without Karpenter the script records PASS (skip).

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

## 5-0. Start the drain monitor (HARD GATE)

Changing the AMI alias makes Karpenter detect drift and replace nodes at once. Start the monitor and
confirm it runs BEFORE editing `${KARPENTER_AMI_VAR}`. Launch with **`run_in_background: true`**:

```bash
python3 scripts/drain_watch.py --phase P5 --scope all --audit-log audit.log
```

It polls Warning events, PDB status and NodeClaim Ready conditions every 30 s and appends `DRAIN-P5`
lines. Wait for `DRAIN-P5 | START` with a pid. Brief `NodeClaimNotReady` WARNs are expected; a
sustained one or any FAIL means STOP.

## 5-0b. Start the service monitor (only when the recipe has `services`)

Same HARD GATE, before editing `${KARPENTER_AMI_VAR}`. Launch with **`run_in_background: true`**:

```bash
python3 scripts/service_watch.py --phase P5 --audit-log audit.log \
  --services-json '<SERVICES_JSON>'
```

It appends `SVC-P5` lines; read them as in nodes.md 4-0b.

## 5-1. Targeted plan, guard and apply

With the monitors running, set only the approved `${KARPENTER_AMI_VAR}` in `${KARPENTER_AMI_FILE}`
(value from the step's `expect`). Use the EC2NodeClass address from the step's targets as
`NODECLASS_ADDRESS`; never put this change in the nodes plan.

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -target="${NODECLASS_ADDRESS}" \
  -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/karpenter.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/karpenter.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log &&
python3 scripts/apply_guard.py --planfile "${TF_DIR}/karpenter.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log &&
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform apply "${TF_DIR}/karpenter.tfplan")
```

Watch the replacement:

```bash
kubectl get nodeclaims -o yaml | grep -A5 "type: Drifted"
kubectl get nodes -l karpenter.sh/nodepool \
  -o custom-columns='NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,READY:.status.conditions[-1].status'
```

## 5-2. Phase 5 gate (always)

During a rollout, the gate returns exit 2 `NEXT: RETRY`; rerun every 30s.
After 30 minutes from the last `APPLY-GUARD | PASS`, it FAILs.

```bash
python3 scripts/phase_gate.py phase5 \
  --target-version "${TARGET_VERSION}" \
  --audit-log audit.log
```

On PASS, finish the step only if the plan has a `karpenter` step:

```bash
python3 scripts/upgrade_plan.py done --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id karpenter
```

Stop the P5 monitors (KillShell, or `--stop-file`). Return to the SKILL.md step loop.
