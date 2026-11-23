"""Deterministic plan approval and audit-driven progression; no infrastructure."""
import json
from pathlib import Path

import pytest

import upgrade_plan as up
from lib import audit_append


def make_plan(tmp_path, order=None, pools=(), language="ko", expected=None, approve=True):
    """Create CLI plans or equivalent custom test plans, with independent step ids."""
    path = tmp_path / "upgrade-plan.json"
    audit = tmp_path / "audit.log"
    order = order or up.default_order(pools)
    doc = dict(schema_version=1, cluster="demo", current_version="1.34", target_version="1.35",
               tf_dir=str(tmp_path), output_language=language, node_pools=list(pools),
               steps=[dict(id=s, type=s, targets=[], expect=(expected or {}).get(s, {})) for s in order],
               deviations=[] if order == up.default_order(pools) else [{"reason": "test order"}])
    path.write_text(json.dumps(doc))
    if approve:
        assert up.main(["approve", "--plan-doc", str(path), "--audit-log", str(audit),
                        "--answer", "승인" if language == "ko" else "approve"]) == 0
    return path, audit


def invoke(command, path, audit, *extra):
    return up.main([command, "--plan-doc", str(path), "--audit-log", str(audit), *extra])


def guard(path, audit, step):
    _, current, meta, _, _ = up.current(path, audit)
    audit_append(str(audit), "APPLY-GUARD", "PASS", up.detail_fields(**meta, step_id=step, step=current["type"]))


@pytest.mark.parametrize("pools,order", [((), ["cp", "full"]), (("Fargate",), ["cp", "full"]),
    (("MNG", "Karpenter"), ["cp", "nodes", "karpenter", "full"])])
def test_create_default_order_and_render(tmp_path, pools, order):
    path = tmp_path / "upgrade-plan.json"
    args = ["create", "--plan-doc", str(path), "--cluster-name", "demo", "--current-version", "1.34",
            "--target-version", "1.35", "--tf-dir", str(tmp_path), "--output-language", "ko",
            "--target", "cp=module.eks", "--expect", "full:alias=ami"]
    for pool in pools:
        args.extend(["--node-pool", pool])
    assert up.main(args) == 0
    doc = json.loads(path.read_text())
    assert [s["type"] for s in doc["steps"]] == order
    assert doc["steps"][0]["targets"] == ["module.eks"]
    assert doc["steps"][-1]["expect"] == {"alias": ["ami"]}
    assert next(tmp_path.glob("upgrade-plan-demo-*.md")).read_text() == up.render(doc)
    before = path.read_bytes()
    assert up.main(args) == 1
    assert path.read_bytes() == before


def test_custom_order_requires_reason_and_renders_it(tmp_path):
    args = ["create", "--plan-doc", str(tmp_path / "upgrade-plan.json"), "--cluster-name", "demo",
            "--current-version", "1.34", "--target-version", "1.35", "--tf-dir", ".",
            "--output-language", "en", "--node-pool", "MNG", "--order", "cp,addon,nodes,full"]
    assert up.main(args) == 1
    assert up.main(args + ["--reason", "separate addon stabilization"]) == 0
    assert "separate addon stabilization" in next(tmp_path.glob("*.md")).read_text()


@pytest.mark.parametrize("language,answer,code", [("ko", "승인", 0), ("ko", "확인", 0), ("ko", "시작", 0),
    ("en", "APPROVE", 0), ("en", "Confirm", 0), ("en", "start", 0), ("ko", "ok", 2),
    ("ko", "승인합니다", 2), ("ko", "approve", 2), ("en", "승인", 2), ("en", "approve ", 2)])
def test_only_exact_language_approval_words(tmp_path, language, answer, code, capsys):
    path, audit = make_plan(tmp_path, language=language, approve=False)
    assert invoke("approve", path, audit, "--answer", answer) == code
    if code:
        assert not audit.exists()
        assert capsys.readouterr().out.endswith("NEXT: ASK_USER exact approval word required\n")
    else:
        assert up.fields(up.records(audit)[0]["detail"])["sha256"] == up.load(path)[2]


def test_done_only_current_step_with_guard_after_previous_completion(tmp_path):
    path, audit = make_plan(tmp_path)
    assert invoke("done", path, audit, "--step-id", "cp") == 1
    guard(path, audit, "full")
    assert invoke("done", path, audit, "--step-id", "full") == 1
    guard(path, audit, "cp")
    assert invoke("done", path, audit, "--step-id", "cp") == 0
    assert invoke("done", path, audit, "--step-id", "full") == 1
    guard(path, audit, "full")
    assert invoke("done", path, audit, "--step-id", "full") == 0
    assert up.context(path, audit)[1] is None


def test_skip_requires_exact_word_reason_and_current_step(tmp_path):
    path, audit = make_plan(tmp_path)
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "ok", "--reason", "external apply") == 2
    assert up.current(path, audit)[1]["id"] == "cp"
    assert invoke("skip", path, audit, "--step-id", "full", "--answer", "승인", "--reason", "wrong") == 1
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "승인", "--reason", " ") == 1
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "승인", "--reason", "external apply") == 0
    assert up.current(path, audit)[1]["id"] == "full"
    assert up.records(audit)[-1]["rule_id"] == "DEVIATION"


def test_next_is_read_only_and_reapproval_does_not_reset_progress(tmp_path, capsys):
    path, audit = make_plan(tmp_path)
    guard(path, audit, "cp")
    invoke("done", path, audit, "--step-id", "cp")
    before = audit.read_bytes()
    assert invoke("approve", path, audit, "--answer", "확인") == 0
    capsys.readouterr()
    assert invoke("next", path, audit) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "full"
    assert audit.read_bytes() == before


@pytest.mark.parametrize("mutation", [lambda d: d.update(schema_version=2),
    lambda d: d["steps"].append(d["steps"][0]), lambda d: d["steps"][0].update(expect={"alias": [None]}),
    lambda d: d["steps"][0].update(type="unknown"), lambda d: d.update(steps=[])])
def test_malformed_json_is_rejected(tmp_path, mutation):
    path, audit = make_plan(tmp_path)
    doc = json.loads(path.read_text())
    mutation(doc)
    path.write_text(json.dumps(doc))
    assert invoke("next", path, audit) == 1


@pytest.mark.parametrize("gate_name", ["plan_check", "apply_guard"])
@pytest.mark.parametrize("edit", [False, True])
def test_guards_refuse_unapproved_or_edited_json(tmp_path, monkeypatch, gate_name, edit, capsys):
    import importlib
    gate = importlib.import_module(gate_name)
    path, audit = make_plan(tmp_path, approve=edit)
    if edit:
        doc = json.loads(path.read_text())
        doc["steps"][0]["expect"] = {"alias": "tampered"}
        path.write_text(json.dumps(doc))
    saved = tmp_path / "saved.tfplan"
    saved.write_bytes(b"saved")
    monkeypatch.setattr(gate, "run_cmd", lambda command: pytest.fail("must fail before external commands"))
    args = ["--planfile", str(saved), "--plan-doc", str(path), "--audit-log", str(audit), "--target-version", "1.35"]
    args.extend(["--tf-dir", str(tmp_path)] if gate_name == "plan_check" else ["--cluster-name", "demo"])
    assert gate.main(args) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP ")
    assert up.records(audit)[-1]["result"] == "FAIL"


def test_cp409_same_plan_cannot_shop_step_types(tmp_path, monkeypatch):
    import subprocess
    import plan_check
    import apply_guard
    path, audit = make_plan(tmp_path, order=["cp", "nodes", "karpenter", "full"],
                            pools=["MNG", "Karpenter"],
                            expected={"nodes": {"alias": "new"}, "karpenter": {"alias": "new"}})
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "승인", "--reason", "CP recovered") == 0
    saved = tmp_path / "nodes.tfplan"
    saved.write_bytes(b"EC2NodeClass plan")
    plan = {"resource_changes": [{"type": "kubernetes_manifest", "change": {"actions": ["update"],
        "before": {"manifest": {"kind": "EC2NodeClass"}}, "after": {"manifest": {
            "kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": "new"}]}}}}}]}
    monkeypatch.setattr(plan_check, "run_cmd", lambda command: subprocess.CompletedProcess(command, 0, json.dumps(plan), ""))
    args = ["--planfile", str(saved), "--plan-doc", str(path), "--target-version", "1.35",
            "--tf-dir", str(tmp_path), "--audit-log", str(audit)]
    assert plan_check.main(args) == 2  # nodes, selected by approved JSON, never by caller
    assert plan_check.main(args) == 2
    assert up.fields(up.records(audit)[-1]["detail"])["step"] == "nodes"
    assert invoke("skip", path, audit, "--step-id", "nodes", "--answer", "승인", "--reason", "no MNG change") == 0
    assert plan_check.main(args) == 1  # even approved advancement cannot relabel the same planfile
    args[1] = str(tmp_path / "karpenter.tfplan")
    Path(args[1]).write_bytes(saved.read_bytes())
    assert plan_check.main(args) == 0
    assert up.fields(up.records(audit)[-1]["detail"])["step"] == "karpenter"
    assert apply_guard.main(["--planfile", str(saved), "--plan-doc", str(path), "--target-version", "1.35",
                             "--cluster-name", "demo", "--audit-log", str(audit)]) == 1


def test_guard_rejects_previous_step_check_for_current_step(tmp_path, monkeypatch, capsys):
    import apply_guard
    path, audit = make_plan(tmp_path)
    saved = tmp_path / "cp.tfplan"
    saved.write_bytes(b"saved")
    _, _, binding, _, _ = up.current(path, audit)
    audit_append(str(audit), "PLAN-CHECK", "PASS", up.detail_fields(**binding, planfile=str(saved),
        sha256=__import__('hashlib').sha256(saved.read_bytes()).hexdigest(), step_id="other", step="cp",
        target_version="1.35", node_changes="false"))
    monkeypatch.setattr(apply_guard, "run_cmd", lambda command: pytest.fail("must fail rule 5"))
    assert apply_guard.main(["--planfile", str(saved), "--plan-doc", str(path), "--target-version", "1.35",
                            "--cluster-name", "demo", "--audit-log", str(audit)]) == 1
    assert "rule 5" in capsys.readouterr().out


@pytest.mark.parametrize('later_result', ['PASS', 'FAIL'])
def test_global_latest_check_cannot_be_hidden_by_other_plan_binding(tmp_path, later_result, capsys):
    import apply_guard
    import hashlib
    path, audit = make_plan(tmp_path)
    saved = tmp_path / 'saved.tfplan'
    saved.write_bytes(b'saved')
    _, _, binding, _, _ = up.current(path, audit)
    meta = dict(**binding, planfile=str(saved), sha256=hashlib.sha256(saved.read_bytes()).hexdigest(),
                step_id='cp', step='cp', target_version='1.35', node_changes='false')
    audit_append(str(audit), 'PLAN-CHECK', 'PASS', up.detail_fields(**meta))
    audit_append(str(audit), 'PLAN-CHECK', later_result,
                 up.detail_fields(**{**meta, 'plan_doc': str(tmp_path / 'other.json'), 'step_id': 'full', 'step': 'full'}))
    assert apply_guard.main(['--planfile', str(saved), '--plan-doc', str(path), '--target-version', '1.35',
                            '--cluster-name', 'demo', '--audit-log', str(audit)]) == 1
    assert 'rule 5' in capsys.readouterr().out


def test_create_render_conflict_rolls_back_new_json_preserving_markdown(tmp_path):
    path, _ = make_plan(tmp_path)
    _, doc, _ = up.load(path)
    existing = tmp_path / 'existing.md'
    existing.write_text('keep human document')
    new_json = tmp_path / 'new.json'
    with pytest.raises(FileExistsError):
        up.write_outputs(new_json, doc, existing)
    assert not new_json.exists()
    assert existing.read_text() == 'keep human document'


def test_check_can_retry_same_file_after_missing_approval_is_resolved(tmp_path, monkeypatch):
    import plan_check
    import subprocess
    path, audit = make_plan(tmp_path, approve=False)
    saved = tmp_path / 'cp.tfplan'
    saved.write_bytes(b'saved')
    args = ['--planfile', str(saved), '--plan-doc', str(path), '--target-version', '1.35',
            '--tf-dir', str(tmp_path), '--audit-log', str(audit)]
    assert plan_check.main(args) == 1
    assert invoke('approve', path, audit, '--answer', '승인') == 0
    monkeypatch.setattr(plan_check, 'run_cmd', lambda cmd: subprocess.CompletedProcess(cmd, 0, '{}', ''))
    assert plan_check.main(args) == 0


def test_reapproval_of_edited_plan_is_refused_once_execution_started(tmp_path, capsys):
    path, audit = make_plan(tmp_path, order=["cp", "nodes", "full"], pools=["MNG"],
                            expected={"nodes": {"release_version": ["old"]}})
    guard(path, audit, "cp")
    assert invoke("done", path, audit, "--step-id", "cp") == 0
    doc = json.loads(path.read_text())
    doc["steps"][1]["expect"] = {"release_version": ["actual"]}
    path.write_text(json.dumps(doc))
    before = audit.read_bytes()
    capsys.readouterr()
    assert invoke("approve", path, audit, "--answer", "승인") == 1
    assert capsys.readouterr().out.startswith("NEXT: STOP")
    moved = tmp_path / "copy.json"
    moved.write_text(path.read_text())
    assert invoke("approve", moved, audit, "--answer", "승인") == 1  # new path does not escape
    assert audit.read_bytes() == before


def test_reapproval_refused_after_deviation_only(tmp_path):
    path, audit = make_plan(tmp_path)
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "승인", "--reason", "x") == 0
    doc = json.loads(path.read_text())
    doc["steps"][1]["targets"] = ["a.b"]
    path.write_text(json.dumps(doc))
    assert invoke("approve", path, audit, "--answer", "승인") == 1


def test_planfile_scan_ignores_other_plan_bindings_in_cumulative_log(tmp_path, monkeypatch):
    import subprocess
    import plan_check
    path, audit = make_plan(tmp_path, order=["cp", "nodes", "full"], pools=["MNG"],
                            expected={"nodes": {"release_version": ["r1"]}})
    assert invoke("skip", path, audit, "--step-id", "cp", "--answer", "승인", "--reason", "x") == 0
    saved = tmp_path / "nodes.tfplan"
    saved.write_bytes(b"plan")
    # An earlier hop/plan revision checked a same-named file as another step.
    audit_append(str(audit), "PLAN-CHECK", "PASS", up.detail_fields(
        planfile=str(saved), sha256="old", step="cp", step_id="cp", plan_doc=str(path), plan_sha256="0" * 64))
    plan = {"resource_changes": []}
    monkeypatch.setattr(plan_check, "run_cmd", lambda c: subprocess.CompletedProcess(c, 0, json.dumps(plan), ""))
    plan_check.main(["--planfile", str(saved), "--plan-doc", str(path), "--target-version", "1.35",
                     "--tf-dir", str(tmp_path), "--audit-log", str(audit)])
    last = up.records(audit)[-1]
    assert "different step_id" not in last["detail"]


def discovery_file(tmp_path, **override):
    found = {"schema_version": 1, "status": "complete", "cluster_name": "demo", "current_version": "1.34",
             "target_version": "1.35", "tf_dir": str(tmp_path), "node_pools": ["MNG", "Karpenter"],
             "module": {"address": "module.eks"},
             "mng": {"present": True, "ami_variable": "mng_ami", "target_release": "1.35.0-20261121"},
             "karpenter": {"present": True, "node_classes": ["kubectl_manifest.node_class"],
                           "alias_variable": "node_alias", "target_alias": "bottlerocket@1.47.0"}}
    found.update(override)
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps(found))
    return path


def test_create_from_discovery_fills_pools_targets_and_ami_expectations(tmp_path):
    path = tmp_path / "upgrade-plan.json"
    assert up.main(["create", "--plan-doc", str(path), "--discovery", str(discovery_file(tmp_path)),
                    "--output-language", "ko"]) == 0
    doc = json.loads(path.read_text())
    assert (doc["cluster"], doc["current_version"], doc["target_version"]) == ("demo", "1.34", "1.35")
    assert doc["node_pools"] == ["MNG", "Karpenter"]
    steps = {s["id"]: s for s in doc["steps"]}
    assert list(steps) == ["cp", "nodes", "karpenter", "full"]
    assert steps["cp"] == {"id": "cp", "type": "cp", "targets": ["module.eks"], "expect": {}}
    assert steps["nodes"]["targets"] == ["module.eks.module.eks_managed_node_group"]
    assert steps["nodes"]["expect"] == {"mng_ami": "1.35.0-20261121", "release_version": ["1.35.0-20261121"]}
    assert steps["karpenter"]["expect"] == {"node_alias": "bottlerocket@1.47.0",
                                            "alias": ["bottlerocket@1.47.0"]}
    assert steps["full"]["expect"] == dict(steps["nodes"]["expect"], **steps["karpenter"]["expect"])


def test_create_cli_flags_override_discovery(tmp_path):
    path = tmp_path / "upgrade-plan.json"
    assert up.main(["create", "--plan-doc", str(path), "--discovery", str(discovery_file(tmp_path)),
                    "--output-language", "en", "--cluster-name", "other", "--node-pool", "Karpenter",
                    "--expect", "karpenter:node_alias=bottlerocket@1.48.0",
                    "--expect", "karpenter:alias=bottlerocket@1.48.0"]) == 0
    doc = json.loads(path.read_text())
    assert doc["cluster"] == "other" and doc["node_pools"] == ["Karpenter"]
    assert doc["steps"][1]["expect"] == {"node_alias": "bottlerocket@1.48.0", "alias": ["bottlerocket@1.48.0"]}


def test_create_from_discovery_with_unknown_ami_asks_user(tmp_path, capsys):
    found = discovery_file(tmp_path, mng={"present": True, "ami_variable": None, "target_release": None})
    path = tmp_path / "upgrade-plan.json"
    assert up.main(["create", "--plan-doc", str(path), "--discovery", str(found),
                    "--output-language", "ko"]) == 2
    assert "NEXT: ASK_USER nodes step" in capsys.readouterr().out
    assert not path.exists()


def test_create_refuses_stopped_discovery_and_missing_values(tmp_path, capsys):
    path = tmp_path / "upgrade-plan.json"
    found = discovery_file(tmp_path, status="stop")
    assert up.main(["create", "--plan-doc", str(path), "--discovery", str(found),
                    "--output-language", "ko"]) == 1
    assert up.main(["create", "--plan-doc", str(path), "--output-language", "ko"]) == 1
    assert "--cluster-name is required" in capsys.readouterr().out
