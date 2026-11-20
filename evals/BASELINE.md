# Eval baseline — current skill (d7cb033), before the flow redesign

Scenario `normal-karpenter-fargate` (Karpenter + Fargate, no MNG, 1.33→1.34). Agent `claude-opus-5-5`, fake user Haiku.
Answer sheet has no entry for "skip the Phase 4 FAIL and continue" on purpose: the baseline records what the skill
does without a human rescue.

| Run | Date (UTC) | Cost | Turns | Wall | Result |
|---|---|---|---|---|---|
| 1 | 2026-11-20 | $2.66 | 3 | 220s | Stopped at Phase 4 FAIL. Agent skipped 4-2 (no MNG) and ran the gate with Karpenter + Fargate nodes still on 1.33 |
| 2 | 2026-11-21 | $3.04 | 3 | 312s | Stopped at Phase 4 FAIL. Agent edited the AMI alias but planned only the MNG module target, so nothing was applied |
| 3 | 2026-11-21 | $2.06 | 2 | 267s | All phase gates PASS. Agent improvised: applied the EC2NodeClass target and restarted Fargate pods (step 7-0) before the Phase 4 gate |

Pass rate 1/3. Same skill and same inputs produce three different Phase 4 paths.

## What it shows

- Phase 4 gate requires every node at the target version, but the skill replaces Karpenter nodes in Phase 5 and
  Fargate nodes in Phase 7-0. For a cluster without MNG, following the text literally fails Phase 4.
- Production runs got past this only by agent improvisation (FAIL → Fargate restart → rerun). Run 3 reproduced it.
- Every run applied with `-auto-approve` after a separate plan (plan ≠ apply; decision 10).

## Harness gaps found by live runs (fixed after the run, regression tests added)

- Run 1: `kubectl get nodes --show-labels`
- Run 3: `kubectl get nodes -L <label>`, `kubectl rollout restart deployment/karpenter`
  (the Karpenter restart failed in run 3; the coredns restart had already refreshed the Fargate node, so the outcome
  did not change)

Run artifacts live in `evals/runs/` (gitignored).
