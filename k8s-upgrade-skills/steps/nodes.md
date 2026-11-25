# Phase 4: managed node group rolling update

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Confirm the step id. Current `type` is `nodes` → run the whole file with its id (default `nodes`).
Otherwise this is the gate-only run (SKILL.md, no MNG in the plan): skip 4-1 → 4-4 and do not run
`done`, but still run 4-0, 4-0b and **4-6, which always runs**.

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

## 4-0. Start the drain monitor (HARD GATE)

Start it and confirm it runs BEFORE any Phase 4 action. If an MNG is already `UPDATING` (triggered by
the Phase 2 apply), start it immediately on entering Phase 4. Launch with **`run_in_background: true`**:

```bash
python3 scripts/drain_watch.py --phase P4 --scope all --audit-log audit.log
```

It polls all-namespace Warning events and PDB `disruptionsAllowed=0` every 30 s and appends `DRAIN-P4`
lines. Wait for `DRAIN-P4 | START` with a pid. Check its output with BashOutput during the roll. A FAIL
line means STOP. A `DisruptionBlocked(PDB)` WARN is transient/normal during a roll: investigate, never
force. Continuous blocking for ≥ 10 min (default `--pdb-fail-after 600`) emits
`DisruptionBlockedSustained(PDB)` FAIL: STOP and investigate, never force.

## 4-0b. Start the service monitor (only when the recipe has `services`)

Same HARD GATE: both monitors run before any Phase 4 action. Serialize the recipe `services` to a JSON
array and launch with **`run_in_background: true`**:

```bash
python3 scripts/service_watch.py --phase P4 --audit-log audit.log \
  --services-json '<SERVICES_JSON>'
```

It polls each service's EndpointSlice ready count (vs `min_endpoints`) and `health_check_url` every
30 s and appends `SVC-P4` lines. A service without `health_check_url` is watched EndpointSlice-only;
a service not found at startup is logged WARN and dropped. A sustained `ready_endpoints < min` or
health failure is real traffic impact: STOP and investigate.

## 4-1. AMI target

Discovery read each node group's amiType and resolved its target `release_version` from public SSM,
keeping the architecture (x86_64 vs arm64). The approved `nodes` step pins it in `expect`
(`${MNG_AMI_VAR}`, `release_version`). `CUSTOM` / `WINDOWS_*` values came from the interview.

## 4-2. Update the MNG AMI variable

```bash
grep -nE "^[[:space:]]*${MNG_AMI_VAR}[[:space:]]*=" "${MNG_AMI_FILE}"
```

Set `${MNG_AMI_VAR}` in `${MNG_AMI_FILE}` to the value in the approved `nodes.expect`; choose no new
expectation at check time. Change only MNG variables here (Karpenter has its own step). A
null/unknown AMI fails. If discovered values differ from the plan, revise the plan and get fresh
approval before execution. Record:

```bash
python3 scripts/audit_event.py \
  --audit-log audit.log \
  --rule-id "PHASE4-AMI" \
  --result "PASS" \
  --detail "${MNG_AMI_VAR}: ${OLD_AMI} → ${NEW_AMI} (amiType=${AMI_TYPE})"
```

## 4-3. Targeted plan

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan \
  -target="${EKS_MODULE}.module.eks_managed_node_group" -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/nodes.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/nodes.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" \
  --tf-dir "${TF_DIR}" --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

`aws_eks_node_group` `release_version` change → expected. Any `-/+` on `aws_eks_node_group` → STOP
and ask the user.

## 4-4. Guard and apply

Call 1: run apply_guard in a separate foreground Bash call.

```bash
python3 scripts/apply_guard.py --planfile "${TF_DIR}/nodes.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
User runs this apply → follow [user-apply.md](user-apply.md) here, then return to this step.
Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
The hook denies `$`/subshells in an apply command.

```bash
cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/nodes.tfplan
```

This starts the MNG roll, typically 10–30 minutes per node group.

## 4-5. Watch the rollout

Poll every 60 s until every MNG node shows the target version:

```bash
kubectl get nodes \
  -o custom-columns='NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,READY:.status.conditions[-1].status'
```

## 4-6. Phase 4 gate (always)

During a rollout, the gate returns exit 2 `NEXT: RETRY`; rerun every 30s.
After 30 minutes from the last `APPLY-GUARD | PASS`, it FAILs.
`PV-AZ` FAIL: follow the zone guidance in `NEXT`, STOP and report; capacity or Terraform changes belong to the operator.

```bash
python3 scripts/phase_gate.py phase4 \
  --cluster-name "${CLUSTER_NAME}" \
  --target-version "${TARGET_VERSION}" \
  --audit-log audit.log
```

The node version/Ready check covers MNG nodes and unlabeled nodes (safe side).
Karpenter nodes (`karpenter.sh/nodepool`) are excluded here and verified in phase5;
Fargate nodes (`eks.amazonaws.com/compute-type=fargate`) are verified in phase7 after 7-0.
Excluded counts appear in the gate detail. Pod classification and FailedEvict checks still cover
all workloads; this gate runs even with no MNG step.

**WARN (exit 2)** lists STALE or TRANSIENT pods; the script only classifies, it deletes nothing:
- STALE: `kubectl delete pod -n <ns> <name>`, then rerun the gate.
- TRANSIENT: wait 60 s, then rerun the gate.
- Never pass a phase with unhealthy pods unclassified. For your own pod queries, never use
  `--field-selector status.phase!=Running` (the EKS API server rejects it); filter JSON output instead.

On PASS, finish the step only if the plan has a `nodes` step:

```bash
python3 scripts/upgrade_plan.py done --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id nodes
```

Stop the P4 monitors (KillShell, or `--stop-file`). Return to the SKILL.md step loop.
