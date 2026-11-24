# Phase 7: final validation and report

```bash
python3 scripts/upgrade_plan.py next --plan-doc "${PLAN_DOC}" --audit-log audit.log
```

It must print `null` (every step done or skipped). A remaining step → return to the SKILL.md step loop.

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

## 7-0. Refresh Fargate workloads (before the gate)

Fargate nodes keep the old kubelet until their pods restart, so after a Control Plane upgrade they fail
the Phase 7 node-version check. The gate cannot fix this; you refresh the pods. Skip when
`${AUTH_PREFIX} aws eks list-fargate-profiles --cluster-name "${CLUSTER_NAME}"` is empty.

1. Find Fargate nodes below the target: `kubectl get nodes -o wide | grep fargate`; every `fargate-*`
   node not at `v${TARGET_VERSION}.x` needs a refresh.
2. Find the Deployments on them: `kubectl get pods -A -o wide | grep <fargate-node-name>`.
3. Restart each owner: `kubectl rollout restart deployment/<name> -n <namespace>`. The usual ones are
   `deployment/coredns -n kube-system` and `deployment/karpenter -n karpenter`; restarting both up
   front lets the gate pass on the first try.
4. Wait until the new Fargate nodes are `Ready` at `v${TARGET_VERSION}.x`.

## 7-1. Phase 7 gate (always)

It reruns the Phase 2/3/4 checks in-process plus EKS Insights. The final node version/Ready check
covers **all** nodes, including Karpenter and Fargate. Phase 4 checks MNG and unlabeled nodes;
Karpenter is verified in phase5, Fargate in phase7 after the refresh in 7-0:

```bash
python3 scripts/phase_gate.py phase7 \
  --cluster-name "${CLUSTER_NAME}" \
  --target-version "${TARGET_VERSION}" \
  --audit-log audit.log
```

- **WARN (exit 2)**: the same STALE/TRANSIENT handling as nodes.md 4-6 (delete STALE, wait for
  TRANSIENT, rerun). A remaining WARN needs an ASK_USER approval word before the report.
- **FAIL (exit 1)**: open [failure.md](failure.md). No completion report.

## 7-2. Report

Write the report only after Phase 7 exit 0, or exit 2 with the user's recorded approval word:

```bash
python3 scripts/report.py --audit-log audit.log --plan-doc "${PLAN_DOC}"
```

It writes `upgrade-report-<cluster>-<YYYYMMDD>.md` (never overwriting: `-2`, `-3`, ...) in the plan's language with every table taken from
audit.log for this approved plan (phase timeline, plan steps, applies, monitor WARN/FAIL events,
deviations, Phase 0 summary, overall result) and appends `REPORT | PASS`. Exit 1 `NEXT: STOP` →
fix the named input (plan path, approval) and rerun; never write the tables yourself.

Then replace only the `<!-- NARRATIVE:... -->` slots, in the output language:
- `summary`: the outcome in 2–3 sentences;
- `troubleshooting`: problems met and actions taken, in time order (`없음`/`None` if none);
- `cluster-state`: query the CP version, node versions and unhealthy pod count now; write a table;
- `next-steps`: follow-ups for each WARN, or `없음`/`None`.

Tell the user the report path and the overall result.
