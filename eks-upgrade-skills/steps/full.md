# Phase 6: full Terraform sync

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

The current step must be `full` (default id `full`); any other step → return to the SKILL.md step loop.
A full plan catches the drift the targeted steps left. AMI expectations come from the approved
`full.expect`. A changed/unknown AMI without an expectation returns exit 1, `NEXT: STOP`:
stop and follow [failure.md](failure.md). Re-approval is refused mid-run; never pass new expectations.

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

## 6-1. Full plan, monitor, guard and apply

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/full.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/full.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Show the plan_check node comparison table to the user before apply.

Only after PLAN-CHECK exits 0, apply the exact saved plan. If the plan changes nodes, a drain monitor
must be running before the guard (`DRAIN-P4`/`DRAIN-P5` START with a live pid and no later STOP).
Phase 4/5 stopped theirs, so restart the matching one with **`run_in_background: true`** (`P5` for
Karpenter nodes, `P4` otherwise) and wait for its START line:

```bash
python3 scripts/drain_watch.py --phase P4 --scope all --audit-log audit.log
```

Call 1: run apply_guard in a separate foreground Bash call.

```bash
python3 scripts/apply_guard.py --planfile "${TF_DIR}/full.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Call 2: only after Call 1 exits 0, use a separate Bash call with `run_in_background: true`.
User runs this apply → follow [user-apply.md](user-apply.md) here, then return to this step.
Substitute actual literal values for `<TF_DIR>` and `<AUTH_PREFIX>` (omit an empty prefix); quote paths as needed.
The hook denies `$`/subshells in an apply command.

```bash
cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/full.tfplan
```

## 6-2. Phase 6 gate (always)

```bash
python3 scripts/phase_gate.py phase6 \
  --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" \
  --tf-var-file "${TF_VAR_FILE}" \
  --audit-log audit.log
```

The gate runs `terraform plan`/`show -json` inside the script, so it cannot inherit a shell prefix:
pass `--auth-prefix`/`--tf-var-file` whenever the recipe sets them (omit them otherwise), or it fails
falsely. After the apply and this gate PASS, finish the step:

```bash
python3 scripts/upgrade_plan.py done --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id full
```

Stop a monitor you restarted here. Return to the SKILL.md step loop (`next` prints `null` → final.md).
