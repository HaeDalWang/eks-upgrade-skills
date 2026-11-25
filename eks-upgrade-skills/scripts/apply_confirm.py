#!/usr/bin/env python3
"""Confirm a user's guarded saved-plan apply from Terraform show JSON (exit 0/1/2)."""
import hashlib
import json
import shlex
import sys
from pathlib import Path

from lib import GateArgumentParser, audit_append, run_cmd
from plan_check import _validate_changes
from upgrade_plan import current, detail_fields, fields, load, records


MISSING = object()


def flatten(module):
    """Flatten managed resource values, preserving complete module/count/for_each addresses."""
    if not isinstance(module, dict):
        raise ValueError("state root/child module must be an object")
    result = {}
    for resource in module.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        address = resource["address"]
        if not isinstance(address, str) or "values" not in resource or address in result:
            raise ValueError("invalid/duplicate managed state resource")
        result[address] = resource["values"]
    for child in module.get("child_modules", []):
        nested = flatten(child)
        if result.keys() & nested.keys():
            raise ValueError("duplicate managed state address")
        result.update(nested)
    return result


def known_equal(after, actual, unknown):
    """Compare known leaves, including nested objects and indexed collections."""
    if unknown is True:
        return True
    if isinstance(after, dict):
        return isinstance(actual, dict) and all(
            known_equal(value, actual.get(key, MISSING),
                        unknown.get(key, False) if isinstance(unknown, dict) else False)
            for key, value in after.items())
    if isinstance(after, list):
        return isinstance(actual, list) and len(after) == len(actual) and all(
            known_equal(value, actual[i], unknown[i] if isinstance(unknown, list)
                        and i < len(unknown) else False) for i, value in enumerate(after))
    return type(after) is type(actual) and after == actual


def judge(plan, state):
    prior = flatten(plan["prior_state"]["values"]["root_module"])
    present = flatten(state["values"]["root_module"])
    planned = [r for r in _validate_changes(plan) if r.get("mode") == "managed"
               and r["change"]["actions"] not in (["no-op"], ["read"])]
    addresses = {r["address"] for r in planned}
    unplanned = sorted(a for a in prior.keys() | present.keys() if a not in addresses
                       and prior.get(a, MISSING) != present.get(a, MISSING))
    counts = dict(planned=len(planned), applied=0, not_started=0, neutral=0,
                  unplanned=len(unplanned))
    if unplanned:
        return 1, "unplanned changes: " + ", ".join(unplanned), counts
    remaining = []
    for resource in planned:
        address, change = resource["address"], resource["change"]
        before, now = prior.get(address, MISSING), present.get(address, MISSING)
        if change["actions"] == ["delete"]:
            applied, started = now is MISSING, now is not MISSING and now == before
        else:
            unknown = change.get("after_unknown") or {}
            applied = now is not MISSING and known_equal(change["after"], now, unknown)
            started = now == before
            # Known fields cannot prove an update/replacement if prior values match too.
            if before is not MISSING and known_equal(change["after"], before, unknown):
                applied, started = False, False
        # show JSON exposes current objects, not the deletion of a deposed instance.
        if "deposed" in resource:
            applied, started = False, False
        status = "applied" if applied else "not_started" if started else "neutral"
        counts[status] += 1
        if status == "not_started":
            remaining.append(address)
        elif status == "neutral" and now != before:
            remaining.append(address)
    if counts["applied"] and not remaining:
        return 0, "all distinguishable planned changes applied (neutral allowed)", counts
    if not counts["applied"]:
        return 2, "ASK_USER whether apply ran / is still running and its last output line", counts
    return 2, ("remaining addresses: " + ", ".join(remaining)
               + "; if apply is still running wait and rerun; otherwise ask for its last output line "
               "and follow the step doc's existing branch (cp.md 2-3-a 409 / 2-3-b timeout cleanup); "
               "no matching branch: steps/failure.md"), counts


def guarded(args, planfile, digest, metadata=None):
    doc, step, binding, rows, boundary = current(args.plan_doc, args.audit_log)
    if metadata is not None:
        metadata.update(binding, step_id=step["id"], step=step["type"])
    if (args.cluster_name != doc["cluster"] or args.target_version != doc["target_version"]
            or str(Path(args.tf_dir).expanduser().resolve()) != doc["tf_dir"]):
        raise ValueError("cluster/target-version/tf-dir differs from approved plan")
    matches = [r for r in records(args.audit_log) if r["rule_id"] == "APPLY-GUARD"
               and fields(r["detail"]).get("planfile")
               and Path(fields(r["detail"])["planfile"]).expanduser().resolve() == planfile]
    if not matches or matches[-1] not in rows[boundary:]:
        raise ValueError("no APPLY-GUARD in current approved step for this planfile")
    row, meta = matches[-1], fields(matches[-1]["detail"])
    expected = dict(**binding, step_id=step["id"], step=step["type"], sha256=digest,
                    cluster_name=args.cluster_name, target_version=args.target_version)
    if row["result"] != "PASS" or any(meta.get(k) != v for k, v in expected.items()):
        raise ValueError("last APPLY-GUARD is not PASS or saved plan hash/scope differs")
    return dict(**binding, step_id=step["id"], step=step["type"])


def show(args, planfile=None):
    command = shlex.split(args.auth_prefix) + ["terraform", "-chdir=" + args.tf_dir, "show", "-json"]
    response = run_cmd(command + ([str(planfile)] if planfile else []))
    if response.returncode:
        raise ValueError("terraform show failed: " + response.stderr.strip())
    try:
        value = json.loads(response.stdout)
    except ValueError as error:
        raise ValueError("terraform show non-JSON: {} {}".format(error, response.stderr.strip()))
    if not isinstance(value, dict):
        raise ValueError("terraform show must return a JSON object: " + response.stderr.strip())
    return value


def main(argv=None):
    parser = GateArgumentParser(description=__doc__)
    for name in ("planfile", "plan-doc", "tf-dir", "cluster-name", "target-version", "audit-log"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--auth-prefix", default="")
    args = parser.parse_args(argv)
    planfile = Path(args.planfile).expanduser().resolve()
    meta = dict(plan_doc=str(Path(args.plan_doc).expanduser().resolve()), plan_sha256="unknown",
                step_id="unknown", step="unknown", planfile=str(planfile), sha256="unknown",
                cluster_name=args.cluster_name, target_version=args.target_version)
    counts = dict(planned=0, applied=0, not_started=0, neutral=0, unplanned=0)
    try:
        meta["sha256"] = hashlib.sha256(planfile.read_bytes()).hexdigest()
        meta.update(guarded(args, planfile, meta["sha256"], meta))
        plan, state = show(args, planfile), show(args)
        code, reason, counts = judge(plan, state)
        if (load(args.plan_doc)[2] != meta["plan_sha256"]
                or hashlib.sha256(planfile.read_bytes()).hexdigest() != meta["sha256"]):
            raise ValueError("approved plan or saved plan changed during confirmation")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        code, reason = 1, str(error)
    result = {0: "PASS", 1: "FAIL", 2: "WARN"}[code]
    try:
        audit_append(args.audit_log, "USER-APPLY", result,
                     detail_fields(**meta, **counts, detail=reason),
                     severity={0: "-", 1: "CRITICAL", 2: "HIGH"}[code])
    except OSError as error:
        print("NEXT: STOP cannot write audit log: {}".format(error))
        return 1
    print("USER-APPLY {}: {}".format(result, reason))
    if code:
        next_step = "STOP steps/failure.md " if code == 1 else ""
        print("NEXT: " + next_step + reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
