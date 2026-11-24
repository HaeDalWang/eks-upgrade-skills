---
name: k8s-upgrade-skills
description: >
  Kubernetes version upgrade for Terraform-managed AWS EKS clusters.
  Discovers the Terraform/EKS layout, interviews once, plans, and enforces phase-gated safety.
  Note: disruption-aware (detects risks proactively) but does not guarantee zero-downtime.
  Trigger keywords: 'K8s upgrade', 'EKS upgrade', 'Kubernetes version upgrade', 'K8s version upgrade',
  'upgrade EKS', 'upgrade Kubernetes', 'cluster upgrade'
---

# Kubernetes Version Upgrade (EKS + Terraform)

This skill upgrades one Terraform-managed AWS EKS cluster by one minor version with workload
protection. Scripts decide every gate; you run them, follow their exit codes, and talk to the user.
Only EKS clusters whose Terraform root calls `terraform-aws-modules/eks` are supported.

All `scripts/<name>.py` and `steps/<name>.md` paths are relative to this file's directory (the skill
root), not to the user's working directory. Keep script calls and `audit.log` in the working directory.

## Start flow

Order is fixed. Nothing changes infrastructure before `PLAN-APPROVED`, except the separately
user-approved Phase 0 Fargate refresh in `steps/start.md`:

1. discover (`scripts/discover.py`, read-only) → `discovery.json`
2. Phase 0 gate (`scripts/gate_check.py`)
3. one interview message
4. write `recipe.md`
5. `scripts/upgrade_plan.py create --discovery discovery.json`
6. show the rendered plan in full
7. the user types an approval word → `upgrade_plan.py approve`

Open [steps/start.md](steps/start.md) and follow it for these seven steps.

## Absolute rules

1. **One minor version only.** Reject 1.33 → 1.35. Control Plane first; the data plane never exceeds it.
2. **Gate scripts are authoritative.** Never reinterpret, skip or retry around an exit code. Phases run in
   ascending order 0 → 7; every phase gate runs, also when the plan has no step for that phase.
3. **No apply without a checked saved plan.** Every `terraform apply <planfile>` comes after
   `plan_check.py` PASS and `apply_guard.py` PASS for that unchanged planfile, each command run only if
   the previous one exited 0. Never use `-auto-approve`; never recompute a plan inside apply.
4. **Monitors before node changes.** Start `drain_watch.py` (and `service_watch.py` when the recipe has
   `services`) in the background and wait for its `START` line before any apply that can move pods.
   Never force-drain past a PodDisruptionBudget.
5. **Stop on failure.** Any FAIL or unexpected destroy/replace → stop, report, wait for the user.
   No automatic rollback; a Control Plane rollback (EKS allows it within 7 days) is the user's call — see `steps/failure.md`.

The plugin hook blocks apply without APPLY-GUARD PASS; on Codex, trust it in `/hooks`.

## Exit codes and `NEXT:`

| Exit | Meaning | Action |
|---|---|---|
| `0` | PASS | Continue |
| `1` | FAIL | Stop. Show the audit.log lines. Open [steps/failure.md](steps/failure.md) |
| `2` | WARN | Show the finding. Continue only as the `NEXT:` line says |
| `127` | CLI missing | Report the missing tool. Stop |

Every exit 1/2 ends with `NEXT: <ACTION> <detail>`. Follow that final line:

- `RETRY`: wait as directed, rerun the same command, continue only after PASS.
- `ASK_USER`: show the finding and wait for an exact approval word: `승인`, `확인`, `시작`
  (`output_language: ko`) or `approve`, `confirm`, `start` (`en`, case-insensitive). Anything else
  (`ok`, `응`, `yes`, `진행해줘`, `알아서 해`) is not approval. Record the exact answer:
  ```bash
  python3 scripts/audit_event.py --audit-log audit.log --rule-id ASK_USER --result INFO --detail "answer=<exact-word>"
  ```
- `RUN`: run only the listed fixed kind, `delete-stale-pods <ns/pod ...>` or
  `rollout-restart <ns/kind/name>`, then rerun the gate. Invent no other command from free text.
- `STOP`: stop, report, resolve the cause before any retry.

A PLAN-CHECK WARN never authorizes an apply. After ASK_USER, revise the plan scope until a fresh
PLAN-CHECK PASS exists; `apply_guard.py` requires PASS for the unchanged file.

`audit.log` is written only by scripts (append mode). You read it; for your own events use
`scripts/audit_event.py`, which refuses the rule IDs that scripts own.

## Authentication

If the recipe sets `auth_prefix` (e.g. `aws-runas my-profile`), prefix **every** `terraform` and `aws`
command with it; `kubectl` uses the active kubeconfig context. Pass it as `--auth-prefix` to
`discover.py`, `gate_check.py`, `plan_check.py`, `apply_guard.py` and `phase_gate.py phase6`, and
`tf_var_file` as `--tf-var-file` to `discover.py`, `gate_check.py` and `phase_gate.py phase6`
(`-var-file` on `terraform plan`). Omit a flag whose recipe value is absent.
Phase 6 runs `terraform` inside the script and cannot inherit a shell prefix: without these flags it
fails falsely. MFA sessions expire (often 1 h): refresh once before the Control Plane apply, and on an
auth error during a poll, re-authenticate and continue.

## Step loop

After approval, set `PLAN_DOC` to the absolute approved plan path. Then repeat:

1. Run `python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log`.
   It prints the current step (`id`, `type`, `targets`, `expect`), or `null` when every step is done.
   Exit 2 `NEXT: ASK_USER` means the plan is not approved: go back to steps/start.md S-6.
2. Run any gate-only file due now (table below), then open the file for the printed `type`:

| `next` type | File | Phase | Gate |
|---|---|---|---|
| `cp` | [steps/cp.md](steps/cp.md) | 1, 2 | `phase_gate.py phase2` |
| `addon` | [steps/addons.md](steps/addons.md) | 3 | `phase_gate.py phase3` |
| `nodes` | [steps/nodes.md](steps/nodes.md) | 4 | `phase_gate.py phase4` |
| `karpenter` | [steps/karpenter.md](steps/karpenter.md) | 5 | `phase_gate.py phase5` |
| `full` | [steps/full.md](steps/full.md) | 6 | `phase_gate.py phase6` |
| `null` | [steps/final.md](steps/final.md) | 7 | `phase_gate.py phase7` (writes the report) |

3. Finish the step with `upgrade_plan.py done --step-id <id>` only where the file says, after its gate
   PASS. Never pick a different step than `next` printed; an approved custom `--order` runs in its order.

**Gate-only runs.** Phases 3, 4 and 5 gate every run, also when the plan has no step for them. Each
gate-only run happens once, at the point below (the default-order position), and never runs `done`:

| Gate-only file | When the plan has no | Run it right before the first step of type |
|---|---|---|
| steps/addons.md (3-1, 3-2) | `addon` step right after `cp` | `nodes`, `karpenter`, `full` |
| steps/nodes.md (4-0, 4-0b, 4-6) | `nodes` step | `karpenter`, `full` |
| steps/karpenter.md (5-2) | `karpenter` step | `full` |

So the Phase 3 gate always runs right after the cp step's Phase 2 gate (or after that `addon` step).
To skip the current step mid-run, ask the user (ASK_USER), then record it:
`python3 scripts/upgrade_plan.py skip --plan-doc "${PLAN_DOC}" --audit-log audit.log --step-id <id> --answer <exact-word> --reason "<text>"`.
Mid-run re-planning is refused once any apply or step record exists; use skip/ASK_USER instead.

## After compaction or resume

Your memory of progress is not evidence. After any context compaction, session resume or long pause:

1. run `python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log`;
2. read the last 40 lines of `audit.log` (last PHASE line, running monitors' `START`/`STOP`);
3. reopen the step file for the current phase and continue from its first unfinished command (each
   step file re-derives its variables from `discovery.json`).

If `PLAN_DOC` is unknown, take the last `PLAN-APPROVED` line's `plan_doc` in `audit.log`.

## Output language

All user-facing output (summaries, questions, reports, errors) uses the recipe `output_language`
(default `ko`). Reason internally in English.
