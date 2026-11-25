# User runs the saved-plan apply

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

This shared procedure inherits the calling step's discovery variables and planfile.

Use this path only when the user says they will run this apply themselves (for example, "내가 칠게").
Return to the calling step's Call 2 when the user says "그냥 너가 해"; a changed plan requires fresh checks.
The agent remains responsible for the checks, monitors, confirmation and phase gate.

1. Run the calling site's `plan_check.py` and then `apply_guard.py` (Call 1), in the same order and
   with the same saved planfile, approved plan and drain monitors. Both must exit 0 before continuing.
   Show the plan_check node comparison table to the user before apply.
2. Show the user the **exact literal command** from that site's Call 2:
   `cd <TF_DIR> && <AUTH_PREFIX> terraform apply <TF_DIR>/<planfile>`.
   Substitute the actual paths, saved planfile and authentication prefix; quote paths, omit an empty
   prefix, and leave no `$`, placeholders or subshells. Never guess an unknown value: ask for it.
   Tell the user to run it in their terminal and reply when it ends. Keep drain/service monitors running.
   Do not execute this apply yourself. Wait for the user's reply (any wording, not only "완료").
3. After the reply, run the confirmation below in a foreground call. Set `PLANFILE` to that site's
   guarded planfile (`cp.tfplan`, `cp-resync.tfplan`, `cleanup.tfplan`, `addon.tfplan`, `nodes.tfplan`,
   `karpenter.tfplan` or `full.tfplan`), using its actual absolute path.

```bash
python3 scripts/apply_confirm.py --planfile "${PLANFILE}" --plan-doc "${PLAN_DOC}" \
  --tf-dir "${TF_DIR}" --auth-prefix "${AUTH_PREFIX}" \
  --cluster-name "${CLUSTER_NAME}" --target-version "${TARGET_VERSION}" --audit-log audit.log
```

4. Follow the exit code and final `NEXT:` literally:
   - 0: return to the calling step's polling and phase gate; `done` only after that gate PASS.
   - 2, `ASK_USER`: ask whether the apply ran / is still running and for its last output line.
   - 2, remaining addresses: if still running, wait and rerun confirmation. Otherwise get the last
     output line and follow the calling step's existing error branch (CP 2-3-a 409, 2-3-b timeout
     cleanup). Never kill a user's terminal process yourself; ask them to stop it if that branch
     requires stopping apply. No matching branch → [failure.md](failure.md).
   - 1: STOP → [failure.md](failure.md), including unplanned resource changes. Never advance the gate.

Only `apply_confirm.py` writes `USER-APPLY`; never use `audit_event.py` for it or write audit.log directly.
