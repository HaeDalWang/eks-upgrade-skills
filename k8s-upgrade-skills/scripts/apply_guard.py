#!/usr/bin/env python3
"""Enforce absolute apply rules using audit.log (exit 0/1).

Rule 5: the last PLAN-CHECK for this exact saved plan must PASS and still match
its hash, step and target. Rule 4: node-changing plans require a live drain
monitor with no later STOP. Rule 3: node changes without a CP version update require ACTIVE target CP.
No Terraform operation is executed here. Any failed rule emits NEXT: STOP.
"""

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from lib import GateArgumentParser, audit_append, parse_audit_line, run_cmd
from plan_check import STEPS, detail_fields

DRAIN_PHASES = {"cp": {"DRAIN-P2"}, "nodes": {"DRAIN-P4"},
                "karpenter": {"DRAIN-P5"}, "full": {"DRAIN-P2", "DRAIN-P4", "DRAIN-P5"}}


def _fields(detail):
    return dict(token.split("=", 1) for token in shlex.split(detail) if "=" in token)


def _records(path):
    return [record for line in Path(path).read_text(encoding="utf-8").splitlines()
            for record in [parse_audit_line(line)] if record]


def _last_check(records, planfile):
    found = None
    for record in records:
        if record["rule_id"] != "PLAN-CHECK":
            continue
        fields = _fields(record["detail"])
        if fields.get("planfile") and Path(fields["planfile"]).expanduser().resolve() == planfile:
            found = record, fields
    if found is None:
        raise ValueError("rule 5: no PLAN-CHECK for this planfile")
    return found


def _live_monitor(records, step):
    """Track START/STOP per monitor pid so a stopped old process cannot authorize."""
    running = {}
    for record in records:
        rule_id = record["rule_id"]
        if rule_id not in DRAIN_PHASES.get(step, set()):
            continue
        detail = record["detail"]
        event = record["result"]
        fields = _fields(detail)
        if event == "START":
            pid = int(fields.get("pid", "0"))
            if pid > 0:
                running[(rule_id, pid)] = True
        elif event == "STOP":
            for key in list(running):
                if key[0] == rule_id and (not fields.get("pid") or str(key[1]) == fields["pid"]):
                    running.pop(key)
    for _, pid in reversed(list(running)):
        try:
            os.kill(pid, 0)
            response = run_cmd(["ps", "-p", str(pid), "-o", "command="])
            if response.returncode == 0 and "drain_watch.py" in response.stdout:
                return True
        except OSError:
            continue
    return False


def check_rules(args, records, planfile, digest):
    check, fields = _last_check(records, planfile)
    if check["result"] != "PASS" or fields.get("sha256") != digest:
        raise ValueError("rule 5: last PLAN-CHECK is not PASS or saved plan hash changed")
    if fields.get("step") != args.step or fields.get("target_version") != args.target_version:
        raise ValueError("rule 5: PLAN-CHECK step/target_version differs from apply request")
    if fields.get("node_changes") not in ("true", "false"):
        raise ValueError("rule 5: PLAN-CHECK missing node_changes metadata")
    node_changes = args.step in ("nodes", "karpenter") or fields["node_changes"] == "true"
    if node_changes and not _live_monitor(records, args.step):
        raise ValueError("rule 4: no live drain_watch START pid without later STOP")
    if node_changes and (args.step != "cp" or fields.get("cp_update") != "true"):
        command = shlex.split(args.auth_prefix) + ["aws", "eks", "describe-cluster",
                    "--name", args.cluster_name, "--output", "json"]
        response = run_cmd(command)
        if response.returncode != 0:
            raise ValueError("rule 3: aws eks describe-cluster failed: " + response.stderr.strip())
        cluster = json.loads(response.stdout).get("cluster", {})
        if cluster.get("status") != "ACTIVE" or cluster.get("version") != args.target_version:
            raise ValueError("rule 3: Control Plane must be ACTIVE at target_version")


def build_parser():
    parser = GateArgumentParser(description=__doc__)
    parser.add_argument("--planfile", required=True)
    parser.add_argument("--step", required=True, choices=STEPS)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--audit-log", required=True)
    parser.add_argument("--auth-prefix", default="")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    planfile = Path(args.planfile).expanduser().resolve()
    digest, message, result = "unknown", "absolute apply rules satisfied", "PASS"
    try:
        digest = hashlib.sha256(planfile.read_bytes()).hexdigest()
        check_rules(args, _records(args.audit_log), planfile, digest)
        if hashlib.sha256(planfile.read_bytes()).hexdigest() != digest:
            raise ValueError("rule 5: saved plan changed while checking apply rules")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        message, result = str(error), "FAIL"
    try:
        audit_append(args.audit_log, "APPLY-GUARD", result,
                     detail_fields(planfile=str(planfile), sha256=digest, step=args.step,
                                   target_version=args.target_version, cluster_name=args.cluster_name,
                                   detail=message), severity="-" if result == "PASS" else "CRITICAL")
    except OSError as error:
        print("NEXT: STOP cannot write audit log: {}".format(error))
        return 1
    print("APPLY-GUARD {}: {}".format(result, message))
    if result != "PASS":
        print("NEXT: STOP {}".format(message))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
