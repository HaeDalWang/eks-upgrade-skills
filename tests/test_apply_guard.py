"""Absolute apply rules validated from synthetic audit records and mocked AWS."""
import hashlib
import json
import os
import subprocess
from argparse import Namespace

import pytest

import apply_guard
from lib import audit_append, parse_audit_line
from plan_check import detail_fields
from test_upgrade_plan import make_plan
import upgrade_plan as up


def setup(tmp_path, step="cp", node_changes=False):
    saved = tmp_path / "saved plan.tfplan"
    saved.write_bytes(b"saved terraform plan")
    audit = tmp_path / "audit.log"
    doc, audit = make_plan(tmp_path, order=list(dict.fromkeys([step, "cp", "full"])))
    _, _, binding, _, _ = up.current(doc, audit)
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    fields = dict(planfile=str(saved), sha256=digest, step=step, step_id=step, **binding,
                  target_version="1.35", node_changes=str(node_changes).lower(),
                  cp_update=str(step == "cp").lower())
    audit_append(str(audit), "PLAN-CHECK", "PASS", detail_fields(**fields))
    args = Namespace(step=step, step_id=step, **binding, target_version="1.35", cluster_name="demo", auth_prefix="")
    return saved, audit, fields, args


def record_check(audit, fields, result="PASS"):
    audit_append(str(audit), "PLAN-CHECK", result, detail_fields(**fields))


def check(saved, audit, fields, args):
    apply_guard.check_rules(args, apply_guard._records(str(audit)), saved, fields["sha256"])


def process_or_cluster(command, state=None, code=0, error=""):
    if command[0] == "ps":
        return subprocess.CompletedProcess(command, 0, "python3 scripts/drain_watch.py", "")
    state = state or {"version": "1.35", "status": "ACTIVE"}
    return subprocess.CompletedProcess(command, code, json.dumps({"cluster": state}), error)


def test_cp_without_node_changes_needs_no_monitor_or_aws(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path)
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command: pytest.fail("AWS must not run"))
    check(saved, audit, fields, args)


@pytest.mark.parametrize("result", ["WARN", "FAIL"])
def test_last_check_must_be_pass(tmp_path, result):
    saved, audit, fields, args = setup(tmp_path)
    record_check(audit, fields, result)
    with pytest.raises(ValueError, match="rule 5"):
        check(saved, audit, fields, args)


@pytest.mark.parametrize("key,value", [("sha256", "changed"), ("step", "full"),
    ("target_version", "1.36"), ("node_changes", "unknown")])
def test_changed_or_mismatched_check_fails_closed(tmp_path, key, value):
    saved, audit, fields, args = setup(tmp_path)
    record_check(audit, {**fields, key: value})
    with pytest.raises(ValueError, match="rule 5"):
        check(saved, audit, fields, args)


def test_check_for_another_plan_does_not_authorize_apply(tmp_path):
    saved, audit, fields, args = setup(tmp_path)
    audit.write_text("")
    record_check(audit, {**fields, "planfile": str(tmp_path / "other.tfplan")})
    with pytest.raises(ValueError, match="no PLAN-CHECK"):
        check(saved, audit, fields, args)


def test_cp_node_group_update_requires_live_phase2_monitor(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)
    audit_append(str(audit), "DRAIN-P2", "START", "pid={}".format(os.getpid()))
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command: process_or_cluster(command) if command[0] == "ps" else pytest.fail("CP skips rule 3"))
    check(saved, audit, fields, args)


def test_dead_monitor_does_not_authorize_apply(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    audit_append(str(audit), "DRAIN-P2", "START", "pid=12345")
    def dead(pid, signal):
        raise ProcessLookupError("gone")
    monkeypatch.setattr(apply_guard.os, "kill", dead)
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)


def test_stopped_monitor_does_not_authorize_apply(tmp_path):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    audit_append(str(audit), "DRAIN-P2", "INFO", "START pid={}".format(os.getpid()))
    audit_append(str(audit), "DRAIN-P2", "INFO", "STOP pid={}".format(os.getpid()))
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)


@pytest.mark.parametrize("step,phase", [("nodes", "P4"), ("karpenter", "P5"), ("full", "P4")])
def test_node_changes_require_active_target_control_plane(tmp_path, monkeypatch, step, phase):
    saved, audit, fields, args = setup(tmp_path, step, node_changes=True)
    audit_append(str(audit), "DRAIN-" + phase, "START", "pid={}".format(os.getpid()))
    def aws(command):
        return process_or_cluster(command)
    monkeypatch.setattr(apply_guard, "run_cmd", aws)
    check(saved, audit, fields, args)
    for state in ({"version": "1.34", "status": "ACTIVE"},
                  {"version": "1.35", "status": "UPDATING"}):
        monkeypatch.setattr(apply_guard, "run_cmd", lambda command:
            process_or_cluster(command, state))
        with pytest.raises(ValueError, match="rule 3"):
            check(saved, audit, fields, args)


def test_guard_cli_records_pass_and_honors_auth_prefix(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path, "nodes", node_changes=True)
    audit_append(str(audit), "DRAIN-P4", "START", "pid={}".format(os.getpid()))
    commands = []
    def aws(command):
        commands.append(command)
        return process_or_cluster(command)
    monkeypatch.setattr(apply_guard, "run_cmd", aws)
    code = apply_guard.main(["--planfile", str(saved), "--plan-doc", str(tmp_path / "upgrade-plan.json"), "--cluster-name", "demo",
        "--target-version", "1.35", "--audit-log", str(audit),
        "--auth-prefix", "aws-vault exec account --"])
    assert code == 0
    record = parse_audit_line(audit.read_text().splitlines()[-1])
    assert record["rule_id"] == "APPLY-GUARD" and record["result"] == "PASS"
    assert commands[0] == ["ps", "-p", str(os.getpid()), "-o", "command="]
    assert commands[1][:4] == ["aws-vault", "exec", "account", "--"]


def test_guard_cli_detects_changed_plan_and_emits_stop(tmp_path, capsys):
    saved, audit, fields, args = setup(tmp_path)
    saved.write_bytes(b"different plan")
    assert apply_guard.main(["--planfile", str(saved), "--plan-doc", str(tmp_path / "upgrade-plan.json"), "--cluster-name", "demo",
        "--target-version", "1.35", "--audit-log", str(audit)]) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")
    assert parse_audit_line(audit.read_text().splitlines()[-1])["result"] == "FAIL"


def test_unreadable_audit_log_fails_closed(tmp_path, capsys):
    saved = tmp_path / "saved.tfplan"
    saved.write_bytes(b"saved")
    audit = tmp_path / "missing-audit.log"
    assert apply_guard.main(["--planfile", str(saved), "--plan-doc", str(tmp_path / "upgrade-plan.json"), "--cluster-name", "demo",
        "--target-version", "1.35", "--audit-log", str(audit)]) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")


def test_aws_failure_blocks_node_apply(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path, "nodes", node_changes=True)
    audit_append(str(audit), "DRAIN-P4", "START", "pid={}".format(os.getpid()))
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command:
        process_or_cluster(command, code=1, error="denied"))
    with pytest.raises(ValueError, match="rule 3"):
        check(saved, audit, fields, args)


def test_restarted_monitor_after_old_stop_can_authorize_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_guard, "run_cmd", process_or_cluster)
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    audit_append(str(audit), "DRAIN-P2", "START", "pid=12345")
    audit_append(str(audit), "DRAIN-P2", "STOP", "pid=12345")
    audit_append(str(audit), "DRAIN-P2", "START", "pid={}".format(os.getpid()))
    check(saved, audit, fields, args)


def test_info_detail_cannot_spoof_a_live_monitor_start(tmp_path):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    audit_append(str(audit), "DRAIN-P2", "INFO", "START pid={}".format(os.getpid()))
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)


def test_full_unknown_node_class_after_cannot_skip_monitor_or_control_plane(tmp_path, monkeypatch):
    from plan_check import classify_plan
    change = {"type": "kubectl_manifest", "change": {"actions": ["update"],
        "before": {"yaml_body": "kind: EC2NodeClass\nspec: {}\n"},
        "after": {"yaml_body": None}, "after_unknown": {"yaml_body": True}}}
    code, _, node_changes = classify_plan({"resource_changes": [change]}, "full", "1.35", {})
    assert code == 2 and node_changes is True
    # Test the guard independently using a synthetic PLAN-CHECK PASS.
    saved, audit, fields, args = setup(tmp_path, "full", node_changes=node_changes)
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)
    audit_append(str(audit), "DRAIN-P4", "START", "pid={}".format(os.getpid()))
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command:
        process_or_cluster(command, {"status": "ACTIVE", "version": "1.34"}))
    with pytest.raises(ValueError, match="rule 3"):
        check(saved, audit, fields, args)


def test_cp_resync_requires_active_target_control_plane(tmp_path, monkeypatch):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    record_check(audit, {**fields, "cp_update": "false"})
    monkeypatch.setattr(apply_guard, "_live_monitor", lambda *args: True)
    for state in ({"version": "1.34", "status": "ACTIVE"},
                  {"version": "1.35", "status": "UPDATING"}):
        monkeypatch.setattr(apply_guard, "run_cmd", lambda command:
            process_or_cluster(command, state))
        with pytest.raises(ValueError, match="rule 3"):
            check(saved, audit, fields, args)
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command:
        subprocess.CompletedProcess(command, 0,
            json.dumps({"cluster": {"version": "1.35", "status": "ACTIVE"}}), ""))
    check(saved, audit, fields, args)


def test_unrelated_live_pid_cannot_authorize_drain_monitor(tmp_path):
    saved, audit, fields, args = setup(tmp_path, node_changes=True)
    audit_append(str(audit), "DRAIN-P2", "START", "pid={}".format(os.getpid()))
    with pytest.raises(ValueError, match="rule 4"):
        check(saved, audit, fields, args)
