# Phase 6: full Terraform sync

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

The current step must be `full` (default id `full`); any other step → stop and recheck the plan.
A full plan catches the drift the targeted steps left. AMI expectations come from the approved
`full.expect`. A changed/unknown AMI without an expectation returns `NEXT: ASK_USER`: handle it with
`skip`/ASK_USER (DEVIATION), because re-approval is refused mid-run. Never pass new expectations.

## 6-1. Full plan, monitor, guard and apply

```bash
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform plan -var-file="${TF_VAR_FILE}" -out="${TF_DIR}/full.tfplan") &&
python3 scripts/plan_check.py --planfile "${TF_DIR}/full.tfplan" --plan-doc "${PLAN_DOC}" \
  --target-version "${TARGET_VERSION}" --tf-dir "${TF_DIR}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log
```

Only after PLAN-CHECK exits 0, apply the exact saved plan. If the plan changes nodes, a drain monitor
must be running before the guard (`DRAIN-P4`/`DRAIN-P5` START with a live pid and no later STOP).
Phase 4/5 stopped theirs, so restart the matching one with **`run_in_background: true`** (`P5` for
Karpenter nodes, `P4` otherwise) and wait for its START line:

```bash
python3 scripts/drain_watch.py --phase P4 --scope all --audit-log audit.log
```

```bash
python3 scripts/apply_guard.py --planfile "${TF_DIR}/full.tfplan" --plan-doc "${PLAN_DOC}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" \
  --auth-prefix "${AUTH_PREFIX}" --audit-log audit.log &&
(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform apply "${TF_DIR}/full.tfplan")
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

Stop a monitor you restarted here. Open [final.md](final.md).
