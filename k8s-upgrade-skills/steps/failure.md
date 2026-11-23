# On any FAIL

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

Note the current step id: it is where the run stopped. Never kill a running `terraform apply`: a killed
apply leaves a stale state lock and partial state. Let it finish, then report. Stop only the inline
monitors (`drain_watch.py` / `service_watch.py`), after the report is written.

## Which failure

| Failed at | Action |
|---|---|
| Phase 0 (`gate_check.py` exit 1) | Inline checklist only (start.md S-2). No report file |
| Before approval (discover/create/approve exit 1) | Report the `NEXT: STOP` cause. No report file |
| Phase 1–7, a gate exit 1, an unexpected destroy, or any stop after approval | Failure report below |

## Failure report

1. Report the exact error, the affected resource and the phase.
2. Do NOT roll back on your own. If the Control Plane already reached the target, tell the user that
   EKS allows a rollback to the previous minor within 7 days of the upgrade completing
   (docs.aws.amazon.com/eks/latest/userguide/rollback-cluster.html) and that it is their decision:
   - Cluster must be `ACTIVE` with no update in progress; Insights in ERROR/UNKNOWN block it.
   - Managed node groups at the target must go back first (`aws eks update-nodegroup-version ... --kubernetes-version <previous>`);
     Fargate nodes cannot be rolled back (delete their Pods first); EKS add-ons stay at their new versions.
   - The rollback is `aws eks update-cluster-version --kubernetes-version <previous>`; afterwards set the
     Terraform version variables back to the previous version so the next plan does not upgrade again.
   Only on the user's exact approval, record it with `audit_event.py --rule-id ROLLBACK`.
3. Write the tables:
   ```bash
   python3 scripts/report.py --audit-log audit.log --plan-doc "${PLAN_DOC}"
   ```
   It writes `upgrade-report-<cluster>-<YYYYMMDD>-FAILED.md` (never overwriting: an existing name gets
   `-2`, `-3`, ...); the result line shows where the run stopped (e.g. `❌ Phase 4에서 실패`). Exit 1
   `NEXT: STOP` → fix the named input and rerun.
4. Fill the `<!-- NARRATIVE:... -->` slots (final.md 7-2). In `summary`, state the cluster's version
   state:
   - Phase 1 or before the cp apply: "업그레이드 미시작 — 클러스터 상태 변경 없음"
   - Phase 2: "⚠️ Control Plane 업그레이드 중 실패. 현재 버전 확인 필요"
   - Phase 3 or later: "⚠️ Control Plane은 {TARGET_VERSION}으로 업그레이드됨. Data Plane은 이전 버전 상태일 수 있음"
5. In `next-steps`: find the root cause before any retry. When the Control Plane is already upgraded
   (Phase 3+), the cluster has mixed versions: guide the user to finish the data-plane upgrade once the
   cause is fixed.

Common causes by gate:

| Gate | Fails when |
|---|---|
| Phase 2 | CP status not ACTIVE or version not the target |
| Phase 3 | an add-on in a bad state, or kube-system pods unhealthy |
| Phase 4 | node version mismatch, FailedEvict, or BLOCKING pods |
| Phase 5 | Karpenter node version mismatch or NotReady |
| Phase 6 | Terraform recreate detected or plan failure (check `--auth-prefix`/`--tf-var-file`) |
| Phase 7 | a sub-check failed or an Insight is not passing; no completion report |
