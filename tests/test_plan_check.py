"""Saved-plan step policies and AMI/version safety checks; no live Terraform."""
import json
import subprocess
from pathlib import Path

import pytest

import plan_check
from lib import parse_audit_line
from test_upgrade_plan import make_plan


def resource(kind, before, after, actions=None, unknown=None):
    return {"address": "module.eks." + kind + ".this", "type": kind,
            "change": {"actions": actions or ["update"], "before": before, "after": after,
                       "after_unknown": unknown or {}}}


def plan(*changes, variables=None):
    return {"resource_changes": list(changes),
            "variables": {key: {"value": value} for key, value in (variables or {}).items()}}


def cluster(target="1.35"):
    return resource("aws_eks_cluster", {"version": "1.34"}, {"version": target})


def test_cp_accepts_version_only_node_update_alongside_cluster_update():
    group = resource("aws_eks_node_group", {"version": "1.34", "release_version": "ami-old"},
                     {"version": "1.35", "release_version": "ami-old"})
    result = plan_check.classify_plan(plan(cluster(), group), "cp", "1.35", {})
    assert result[0] == 0 and result[2] is True
    assert plan_check.classify_plan(plan(group), "cp", "1.35", {})[0] == 0


@pytest.mark.parametrize("kind,step,expected", [("time_sleep", "cp", 0),
    ("time_sleep", "full", 0), ("aws_eks_node_group", "nodes", 2),
    ("aws_security_group", "full", 2), ("aws_eks_cluster", "cp", 2)])
def test_only_allowlisted_replacements_are_accepted(kind, step, expected):
    change = resource(kind, {"version": "1.35", "release_version": "ami"},
                      {"version": "1.35", "release_version": "ami"}, ["delete", "create"])
    result = plan_check.classify_plan(plan(change), step, "1.35", {"release_version": "ami"})
    assert result[0] == expected


def test_cp_rejects_release_version_update_even_alongside_cp_upgrade():
    group = resource("aws_eks_node_group", {"release_version": "old"}, {"release_version": "new"})
    assert plan_check.classify_plan(plan(cluster(), group), "cp", "1.35", {})[0] == 2


@pytest.mark.parametrize("target", ["1.34", None])
def test_cluster_version_must_match_target(target):
    assert plan_check.classify_plan(plan(cluster(target)), "cp", "1.35", {})[0] == 1


@pytest.mark.parametrize("actual,unknown", [(None, False), ("ami", True), ("wrong", False)])
def test_nodes_reject_missing_unknown_or_unexpected_release_version(actual, unknown):
    group = resource("aws_eks_node_group", {"release_version": "old"}, {"release_version": actual},
                     unknown={"release_version": unknown})
    assert plan_check.classify_plan(plan(group), "nodes", "1.35", {"release_version": "ami"})[0] == 1


def test_nodes_need_explicit_ami_expectation():
    group = resource("aws_eks_node_group", {"release_version": "old"}, {"release_version": "ami"})
    assert plan_check.classify_plan(plan(group), "nodes", "1.35", {})[0] == 1


def test_nodes_validate_variable_and_resolved_release_independently():
    group = resource("aws_eks_node_group", {"release_version": "old"}, {"release_version": "resolved"})
    expected = {"ami_alias": "alias", "release_version": "resolved"}
    assert plan_check.classify_plan(plan(group, variables={"ami_alias": "alias"}),
                                    "nodes", "1.35", expected)[0] == 0
    assert plan_check.classify_plan(plan(group, variables={"ami_alias": "wrong"}),
                                    "nodes", "1.35", expected)[0] == 1


def test_karpenter_alias_is_checked_against_expected_variable():
    manifest = {"kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": "al2023@v1"}]}}
    change = resource("kubernetes_manifest", {}, {"manifest": manifest})
    data = plan(change, variables={"ami_alias": "al2023@v1"})
    assert plan_check.classify_plan(data, "karpenter", "1.35", {"ami_alias": "al2023@v1"})[0] == 0
    assert plan_check.classify_plan(data, "karpenter", "1.35", {"ami_alias": "al2023@wrong"})[0] == 1


def test_addon_step_does_not_allow_cluster_update():
    addon = resource("aws_eks_addon", {"addon_version": "old"}, {"addon_version": "new"})
    assert plan_check.classify_plan(plan(addon), "addon", "1.35", {})[0] == 0
    assert plan_check.classify_plan(plan(cluster()), "addon", "1.35", {})[0] == 2


@pytest.mark.parametrize("data", [[], {"resource_changes": {}},
    {"resource_changes": [{"type": "aws_eks_cluster", "change": {"actions": ["destroy"]}}]}])
def test_malformed_show_json_fails_closed(data):
    with pytest.raises(ValueError):
        plan_check.classify_plan(data, "full", "1.35", {})


def invoke(tmp_path, monkeypatch, data, step="cp", extra=None, expected=None):
    saved = tmp_path / "a plan.tfplan"
    saved.write_bytes(b"saved terraform plan")
    audit = tmp_path / "audit.log"
    commands = []
    def show(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
    monkeypatch.setattr(plan_check, "run_cmd", show)
    doc, audit = make_plan(tmp_path, order=list(dict.fromkeys([step, "cp", "full"])), expected={step: expected or {}})
    argv = ["--planfile", str(saved), "--plan-doc", str(doc), "--target-version", "1.35",
            "--tf-dir", str(tmp_path), "--audit-log", str(audit)] + (extra or [])
    code = plan_check.main(argv)
    return code, saved, audit, commands


def test_plan_check_hash_metadata_and_auth_prefix(tmp_path, monkeypatch):
    code, saved, audit, commands = invoke(tmp_path, monkeypatch, plan(cluster()),
        extra=["--auth-prefix", "aws-vault exec account --"])
    assert code == 0
    record = parse_audit_line(audit.read_text().splitlines()[-1])
    assert record["rule_id"] == "PLAN-CHECK" and record["result"] == "PASS"
    import shlex
    fields = dict(word.split("=", 1) for word in shlex.split(record["detail"]))
    assert Path(fields["planfile"]) == saved and len(fields["sha256"]) == 64
    assert fields["step"] == "cp" and fields["target_version"] == "1.35"
    assert commands[0][:4] == ["aws-vault", "exec", "account", "--"]
    assert commands[0][-3:] == ["show", "-json", str(saved)]


def test_unexpected_replace_emits_ask_user(tmp_path, monkeypatch, capsys):
    change = resource("aws_security_group", {}, {}, ["delete", "create"])
    code, _, _, _ = invoke(tmp_path, monkeypatch, plan(change), "full")
    assert code == 2
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: ASK_USER ")


def test_version_mismatch_emits_stop(tmp_path, monkeypatch, capsys):
    assert invoke(tmp_path, monkeypatch, plan(cluster("1.34")))[0] == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")


def test_plan_mutated_during_show_cannot_pass(tmp_path, monkeypatch):
    saved = tmp_path / "saved.tfplan"
    saved.write_bytes(b"original")
    def show(command):
        saved.write_bytes(b"replaced")
        return subprocess.CompletedProcess(command, 0, json.dumps(plan(cluster())), "")
    monkeypatch.setattr(plan_check, "run_cmd", show)
    doc, audit = make_plan(tmp_path)
    assert plan_check.main(["--planfile", str(saved), "--plan-doc", str(doc), "--target-version", "1.35",
        "--tf-dir", str(tmp_path), "--audit-log", str(tmp_path / "audit.log")]) == 1


def test_time_sleep_delete_only_is_not_an_allowed_replacement():
    change = resource("time_sleep", {"triggers": {}}, None, ["delete"])
    assert plan_check.classify_plan(plan(change), "cp", "1.35", {})[0] == 2


def test_cp_unknown_release_version_is_not_version_only_update():
    group = resource("aws_eks_node_group", {"version": "1.34", "release_version": "ami-old"},
        {"version": "1.35", "release_version": None}, unknown={"release_version": True})
    assert plan_check.classify_plan(plan(cluster(), group), "cp", "1.35", {})[0] == 2


def test_cp_unknown_security_change_is_not_version_only_update():
    cp = resource("aws_eks_cluster", {"version": "1.34", "vpc_config": [{"endpoint_public_access": False}]},
        {"version": "1.35", "vpc_config": None}, unknown={"vpc_config": [{"endpoint_public_access": True}]})
    assert plan_check.classify_plan(plan(cp), "cp", "1.35", {})[0] == 2


def test_cp_computed_outputs_can_be_unknown_on_version_update():
    cp = resource("aws_eks_cluster", {"version": "1.34", "platform_version": "old"},
        {"version": "1.35", "platform_version": None}, unknown={"platform_version": True})
    assert plan_check.classify_plan(plan(cp), "cp", "1.35", {})[0] == 0


def test_explicit_resolved_release_must_match_even_if_alias_matches():
    group = resource("aws_eks_node_group", {"release_version": "old"}, {"release_version": "alias"})
    data = plan(group, variables={"ami_alias": "alias"})
    expected = {"ami_alias": "alias", "release_version": "resolved"}
    assert plan_check.classify_plan(data, "nodes", "1.35", expected)[0] == 1


def test_show_failure_records_fail_and_stop(tmp_path, monkeypatch, capsys):
    saved, audit = tmp_path / "saved", tmp_path / "audit.log"
    saved.write_bytes(b"saved")
    monkeypatch.setattr(plan_check, "run_cmd", lambda command:
        subprocess.CompletedProcess(command, 1, "", "could not decode plan"))
    doc, audit = make_plan(tmp_path)
    code = plan_check.main(["--planfile", str(saved), "--plan-doc", str(doc), "--target-version", "1.35",
        "--tf-dir", str(tmp_path), "--audit-log", str(audit)])
    assert code == 1 and parse_audit_line(audit.read_text().splitlines()[-1])["result"] == "FAIL"
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")


def test_missing_plan_file_records_failure_without_running_terraform(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_check, "run_cmd", lambda command: pytest.fail("must not run"))
    audit = tmp_path / "audit.log"
    doc, audit = make_plan(tmp_path)
    assert plan_check.main(["--planfile", str(tmp_path / "missing"), "--plan-doc", str(doc),
        "--target-version", "1.35", "--tf-dir", str(tmp_path), "--audit-log", str(audit)]) == 1
    assert parse_audit_line(audit.read_text().splitlines()[-1])["result"] == "FAIL"


@pytest.mark.parametrize("manifest_field,body", [("yaml_body", "kind: EC2NodeClass\nspec: {}\n"),
    ("manifest", {"kind": "EC2NodeClass", "spec": {}})])
def test_full_unknown_node_class_after_still_requires_node_protections(manifest_field, body):
    kind = "kubectl_manifest" if manifest_field == "yaml_body" else "kubernetes_manifest"
    change = resource(kind, {manifest_field: body}, {manifest_field: None},
                      unknown={manifest_field: True})
    code, _, node_changes = plan_check.classify_plan(plan(change), "full", "1.35", {})
    assert code == 2 and node_changes is True


def test_full_unknown_manifest_identity_fails_closed():
    change = resource("kubectl_manifest", {}, {"yaml_body": None},
                      unknown={"yaml_body": True})
    assert plan_check.classify_plan(plan(change), "full", "1.35", {})[0] == 1


def test_full_unknown_manifest_kind_cannot_reuse_before_non_node_identity():
    change = resource("kubernetes_manifest", {"manifest": {"kind": "ConfigMap"}},
        {"manifest": {"kind": None}}, unknown={"manifest": {"kind": True}})
    assert plan_check.classify_plan(plan(change), "full", "1.35", {})[0] == 1


def test_full_non_node_manifest_with_known_kind_can_have_unknown_data():
    change = resource("kubernetes_manifest", {"manifest": {"kind": "ConfigMap", "data": {}}},
        {"manifest": {"kind": "ConfigMap", "data": None}},
        unknown={"manifest": {"kind": False, "data": True}})
    code, _, node_changes = plan_check.classify_plan(plan(change), "full", "1.35", {})
    assert code == 0 and node_changes is False


def test_cp_resync_accepts_node_version_at_target_only():
    for version, code in [("1.35", 0), ("1.36", 2)]:
        group = resource("aws_eks_node_group", {"version": "1.34"}, {"version": version})
        assert plan_check.classify_plan(plan(group), "cp", "1.35", {})[0] == code


@pytest.mark.parametrize("step", plan_check.STEPS)
def test_deposed_delete_allowed_only_in_full(step):
    deleted = resource("aws_security_group", {}, None, ["delete"])
    deleted["deposed"] = "old-object"
    assert plan_check.classify_plan(plan(deleted), step, "1.35", {})[0] == (0 if step == "full" else 2)
    del deleted["deposed"]
    assert plan_check.classify_plan(plan(deleted), step, "1.35", {})[0] == 2


def test_repeated_resolved_ami_expectations_match_multiple_resources(tmp_path, monkeypatch):
    groups = [resource("aws_eks_node_group", {"release_version": "old"},
                       {"release_version": value}) for value in ("a", "b")]
    classes = [resource("kubernetes_manifest", {}, {"manifest": {
        "kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": value}]}}})
        for value in ("x", "y")]
    expected = {"release_version": ["a", "b"], "alias": ["x", "y"]}
    assert invoke(tmp_path, monkeypatch, plan(*groups, *classes), "full", expected=expected)[0] == 0
    groups[0]["change"]["after"]["release_version"] = "x"
    assert invoke(tmp_path, monkeypatch, plan(*groups, *classes), "full", expected=expected)[0] == 1


def test_conflicting_named_ami_expectations_are_usage_error(tmp_path, capsys):
    import upgrade_plan
    code = upgrade_plan.main(["create", "--cluster-name", "demo", "--current-version", "1.34",
        "--target-version", "1.35", "--tf-dir", str(tmp_path), "--output-language", "ko",
        "--plan-doc", str(tmp_path / "upgrade-plan.json"), "--expect", "cp:ami_var=a", "--expect", "cp:ami_var=b"])
    assert code == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")


@pytest.mark.parametrize("kind", ["aws_eks_node_group", "kubernetes_manifest"])
@pytest.mark.parametrize("unknown", [False, True])
def test_full_ami_change_without_kind_expectation_asks_user(tmp_path, monkeypatch, capsys, kind, unknown):
    if kind == "aws_eks_node_group":
        change = resource(kind, {"release_version": "old"}, {"release_version": "new"},
                          unknown={"release_version": unknown})
    else:
        before = {"kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": "old"}]}}
        after = {"kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": "new"}]}}
        change = resource(kind, {"manifest": before}, {"manifest": after},
                          unknown={"manifest": unknown})
    assert invoke(tmp_path, monkeypatch, plan(change), "full")[0] == 2
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: ASK_USER ")


def test_full_unchanged_ami_without_expectation_is_allowed():
    group = resource("aws_eks_node_group", {"version": "1.34", "release_version": "same"},
                     {"version": "1.35", "release_version": "same"})
    assert plan_check.classify_plan(plan(group), "full", "1.35", {})[0] == 0


def test_ami_expectations_for_other_kind_do_not_authorize_karpenter():
    change = resource("kubernetes_manifest", {}, {"manifest": {
        "kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": "a"}]}}})
    assert plan_check.classify_plan(plan(change), "karpenter", "1.35",
                                    {"release_version": {"a"}})[0] == 1
