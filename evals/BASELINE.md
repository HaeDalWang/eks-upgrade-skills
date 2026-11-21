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

## Batch 1 (2026-11-21) — three single-hazard scenarios

Runs that stopped on an API/network error (`infra_error`) or on a harness defect are listed but not counted.

| Scenario | Topology | Valid runs | Pass | Not counted |
|---|---|---|---|---|
| `cp-409` | Karpenter | 3 | 3 | — |
| `addon-sequential` | Karpenter | 3 | 2 | 2 infra_error (API timeout, DNS) |
| `mng-rolling` | MNG | 2 | 2 | 1 fake-user NONE on a plain approval (fixed); 1 shim gap `nodegroup.health` (all gates PASS, fixed) |

Costs per valid run $1.7–$2.9 (one $6.42 figure was an accounting bug: cumulative result events counted twice; fixed).

### What it shows

- The Phase 4 hole is not Fargate-specific. For every Karpenter run (`normal`, `cp-409`, `addon-sequential`; 9 runs),
  the outcome follows one choice: runs that applied `kubectl_manifest.karpenter_default_node_class` before the Phase 4
  gate passed (6), runs that followed the text and replaced Karpenter nodes in Phase 5 failed Phase 4 (3).
- The 409 conflict (`cp-409`) and sequential add-on updates (`addon-sequential`: PHASE3-ADDON WARN → wait → rerun →
  PASS) were handled in every valid run.
- MNG rolling follows the text and passes; Phase 5 records PASS when Karpenter CRDs are absent.
- Approval is requested before Phase 0 results are shown (all scenarios).

Run artifacts live in `evals/runs/` (gitignored).

## Batch 2 — scenario definitions only

`fargate-linger`, `phase0-transient-block`, `service-blip`, `unexpected-replace`,
`phase0-critical`, `ok-approval`을 추가했다. 로컬 CLI transition 및 합성 passing/failing
fixture만 검증하며 **live run은 수행하지 않았다**. 결과/비용/통과율은 아직 모름이다.
현재 skill 문서는 변경하지 않았으며 근거와 Rulings는 README 및 각 JSON에 있다.
