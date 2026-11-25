"""User apply confirmation from the two terraform show JSON formats, without Terraform."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import apply_confirm as confirm
from lib import audit_append
from test_apply_guard import setup
import upgrade_plan as up

ADDRESS = 'module.eks.module.pool["blue"].aws_eks_node_group.this[0]'
OTHER = 'module.storage[0].aws_s3_bucket.logs["access"]'


def state(values):
    return {"values": {"root_module": {"child_modules": [{"address": "module.eks", "resources": [
        {"address": address, "mode": "managed", "values": value} for address, value in values.items()]}]}}}


def fixture(tmp_path, monkeypatch, actions=None, after=None, unknown=None):
    saved, audit, meta, _ = setup(tmp_path)
    meta["cluster_name"] = "demo"
    audit_append(str(audit), "APPLY-GUARD", "PASS", up.detail_fields(**meta))
    before = {"version": "1.34", "id": "old"}
    change = {"actions": actions or ["update"], "before": before,
              "after": after if after is not None else {"version": "1.35", "id": "new"},
              "after_unknown": unknown or {}}
    plan = {"prior_state": state({ADDRESS: before, OTHER: {"name": "logs"}}),
            "resource_changes": [{"address": ADDRESS, "mode": "managed", "type": "aws_eks_node_group", "change": change}]}
    now = state({ADDRESS: change["after"], OTHER: {"name": "logs"}})
    commands = []
    def run(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(plan if len(commands) % 2 else now), "")
    monkeypatch.setattr(confirm, "run_cmd", run)
    args = ["--planfile", str(saved), "--plan-doc", str(tmp_path / "upgrade-plan.json"),
            "--tf-dir", str(tmp_path), "--cluster-name", "demo", "--target-version", "1.35",
            "--auth-prefix", "aws-vault exec example --", "--audit-log", str(audit)]
    return args, saved, audit, meta, plan, now, commands


def invoke(f, expected):
    args, _, audit, _, _, _, _ = f
    count = sum(r["rule_id"] == "USER-APPLY" for r in up.records(audit))
    assert confirm.main(args) == expected
    rows = [r for r in up.records(audit) if r["rule_id"] == "USER-APPLY"]
    assert len(rows) == count + 1
    assert rows[-1]["result"] == {0: "PASS", 1: "FAIL", 2: "WARN"}[expected]
    return up.fields(rows[-1]["detail"])


def test_all_applied_and_auth_chdir(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    assert invoke(f, 0)["applied"] == "1"
    assert f[-1][0][:6] == ["aws-vault", "exec", "example", "--", "terraform", "-chdir=" + str(tmp_path)]
    assert f[-1][1][-2:] == ["show", "-json"]


def test_none_applied_asks_user(tmp_path, monkeypatch, capsys):
    f = fixture(tmp_path, monkeypatch)
    f[5].update(copy.deepcopy(f[4]["prior_state"]))
    invoke(f, 2)
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: ASK_USER")


def test_mixed_lists_remaining(tmp_path, monkeypatch, capsys):
    f = fixture(tmp_path, monkeypatch)
    extra = copy.deepcopy(f[4]["resource_changes"][0]); extra["address"] = OTHER
    extra["change"].update(before={"name": "logs"}, after={"name": "new"})
    f[4]["resource_changes"].append(extra)
    invoke(f, 2)
    assert OTHER in capsys.readouterr().out


def test_neutral_differing_from_before_and_after_is_remaining(tmp_path, monkeypatch):
    # Half-applied or out-of-band edited planned resource must never count as PASS.
    f = fixture(tmp_path, monkeypatch)
    half = copy.deepcopy(f[4]["resource_changes"][0]); half["address"] = OTHER
    half["change"].update(before={"name": "logs"}, after={"name": "logs-v2"})
    f[4]["resource_changes"].append(half)
    f[5]["values"]["root_module"]["child_modules"][0]["resources"][1]["values"] = {"name": "elsewhere"}
    detail = invoke(f, 2)
    assert detail["applied"] == "1" and OTHER in detail["detail"]


@pytest.mark.parametrize("mutation", ["changed", "new", "disappeared"])
def test_unplanned_resources_stop(tmp_path, monkeypatch, capsys, mutation):
    f = fixture(tmp_path, monkeypatch)
    values = {ADDRESS: {"version": "1.35", "id": "new"}}
    if mutation != "disappeared":
        values[OTHER] = {"name": "edited" if mutation == "changed" else "logs"}
    if mutation == "new":
        values['module.extra[0].aws_s3_bucket.new'] = {"name": "new"}
    f[5].update(state(values))
    invoke(f, 1)
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP steps/failure.md")


@pytest.mark.parametrize("actions", [["delete"], ["create"], ["delete", "create"], ["create", "delete"]])
def test_delete_create_and_replace(tmp_path, monkeypatch, actions):
    f = fixture(tmp_path, monkeypatch, actions=actions)
    if actions == ["delete"]:
        f[4]["resource_changes"][0]["change"]["after"] = None
        f[5].update(state({OTHER: {"name": "logs"}}))
    if actions == ["create"]:
        f[4]["prior_state"] = state({OTHER: {"name": "logs"}})
        f[4]["resource_changes"][0]["change"]["before"] = None
    invoke(f, 0)
    f[5].update(copy.deepcopy(f[4]["prior_state"]))
    invoke(f, 2)


def test_unknown_nested_leaves_ignored_and_sensitive_values_compared(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch, after={"version": "1.35", "secret": "expected", "items": [{"id": None, "name": "fixed"}]},
                unknown={"items": [{"id": True}]})
    f[5].update(state({ADDRESS: {"version": "1.35", "secret": "expected", "items": [{"id": "resolved", "name": "fixed"}]}, OTHER: {"name": "logs"}}))
    invoke(f, 0)
    f[5]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"]["secret"] = "wrong"
    invoke(f, 2)


def test_indistinguishable_replacement_is_neutral(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch, actions=["delete", "create"], after={"version": "1.34", "id": None}, unknown={"id": True})
    assert invoke(f, 2)["neutral"] == "1"


def test_neutral_allowed_with_one_applied_and_data_ignored(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    neutral = copy.deepcopy(f[4]["resource_changes"][0]); neutral["address"] = OTHER
    neutral["change"].update(after={"name": "logs"}, before={"name": "logs"})
    f[4]["resource_changes"].append(neutral)
    f[4]["resource_changes"].append({**neutral, "mode": "data", "address": "data.aws_eks_cluster.this"})
    f[5]["values"]["root_module"]["child_modules"][0]["resources"].append({"mode": "data", "address": "data.aws_eks_cluster.this", "values": {"version": "changed"}})
    assert invoke(f, 0)["neutral"] == "1"


@pytest.mark.parametrize("mutation", ["missing", "fail", "hash", "forged", "old", "target", "step", "unapproved"])
def test_guard_preconditions(tmp_path, monkeypatch, mutation):
    f = fixture(tmp_path, monkeypatch)
    _, saved, audit, meta, _, _, _ = f
    if mutation in ("missing", "forged", "old"):
        audit.write_text('\n'.join(line for line in audit.read_text().splitlines() if 'APPLY-GUARD' not in line) + '\n')
    if mutation == "fail":
        audit_append(str(audit), "APPLY-GUARD", "FAIL", up.detail_fields(**meta))
    elif mutation == "hash":
        saved.write_bytes(b"changed")
    elif mutation == "forged":
        result = subprocess.run([sys.executable, str(Path(confirm.__file__).with_name("audit_event.py")),
            "--audit-log", str(audit), "--rule-id", "APPLY-GUARD", "--result", "PASS", "--detail", up.detail_fields(**meta)], capture_output=True)
        assert result.returncode == 1
    elif mutation == "old":
        lines = audit.read_text().splitlines()
        audit_append(str(audit), "APPLY-GUARD", "PASS", up.detail_fields(**meta))
        guard = audit.read_text().splitlines()[-1]
        audit.write_text(guard + '\n' + '\n'.join(lines) + '\n')
    elif mutation in ("target", "step"):
        meta["target_version" if mutation == "target" else "step_id"] = "other"
        audit_append(str(audit), "APPLY-GUARD", "PASS", up.detail_fields(**meta))
    elif mutation == "unapproved":
        audit.write_text('')
    invoke(f, 1)
    assert not f[-1]


def test_cumulative_other_run_and_target_do_not_authorize(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    old = {**f[3], "plan_sha256": "old", "target_version": "1.34"}
    audit_append(str(f[2]), "APPLY-GUARD", "PASS", up.detail_fields(**old))
    invoke(f, 1)
    audit_append(str(f[2]), "APPLY-GUARD", "PASS", up.detail_fields(**f[3]))
    invoke(f, 0)


@pytest.mark.parametrize("nonjson", [False, True])
def test_show_failure_records_reason_once(tmp_path, monkeypatch, capsys, nonjson):
    f = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(confirm, "run_cmd", lambda command: subprocess.CompletedProcess(command, 0 if nonjson else 1, 'broken', 'backend unavailable'))
    invoke(f, 1)
    assert 'backend unavailable' in capsys.readouterr().out


def test_deposed_delete_is_neutral_when_current_replacement_exists(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch, actions=["delete"])
    f[4]["resource_changes"][0].update(deposed="old-object")
    f[4]["resource_changes"][0]["change"]["after"] = None
    assert invoke(f, 2)["neutral"] == "1"


@pytest.mark.parametrize("boundary", ["STEP-DONE", "DEVIATION"])
def test_guard_before_step_boundary_cannot_authorize_reused_planfile(tmp_path, monkeypatch, boundary):
    f = fixture(tmp_path, monkeypatch)
    _, _, audit, meta, _, _, _ = f
    audit_append(str(audit), boundary, "PASS" if boundary == "STEP-DONE" else "SKIP",
                 up.detail_fields(**{k: v for k, v in meta.items() if k in ("plan_doc", "plan_sha256", "step_id", "step")}))
    invoke(f, 1)
    assert not f[-1]
