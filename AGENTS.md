# AGENTS.md

Rules for agents changing this repo. Commands and architecture are in CLAUDE.md.

- Python 3.9, stdlib only. Gate scripts exit 0/1/2/127 and print a final `NEXT:` line on non-zero; reuse `scripts/lib.py`.
- Only scripts write audit.log. A script that trusts an audit line must reject the same rule ID forged via `audit_event.py`.
- Editing a skill doc must not change when a phase gate runs. A gate runs even when the plan has no matching step.
- When a value cannot be found, record it as `unknown` with the reason; never guess a default.
- Tests for audit.log readers must cover cumulative logs from earlier runs and other target versions.
- Live evals (`K8S_EVAL=1`) cost money and are run by the maintainer, not by workers. Judge a live run by its `grade.json` FAIL list; a green pytest only means the run finished.
- Public repo: no customer names, people's names, account IDs or costs from real upgrades in code, tests, commits or docs.
