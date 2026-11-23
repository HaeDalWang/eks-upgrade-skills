"""report.py: deterministic report tables scoped to the current approved plan; no infrastructure."""
import upgrade_plan as up
import report
from lib import audit_append, parse_audit_line
from test_upgrade_plan import make_plan, seed_preflight


def phase(audit, number, result):
    audit_append(str(audit), "PHASE{}".format(number), "ENTER", "phase_gate start")
    audit_append(str(audit), "PHASE{}".format(number), result, "exit={}".format({"PASS": 0, "FAIL": 1, "WARN": 2}[result]))


def finish(path, audit, step):
    _, current, meta, _, _ = up.current(path, audit)
    audit_append(str(audit), "APPLY-GUARD", "PASS", up.detail_fields(**meta, step_id=step, step=current["type"],
                                                                         planfile="/w/{}.tfplan".format(step)))
    assert up.main(["done", "--plan-doc", str(path), "--audit-log", str(audit), "--step-id", step]) == 0


def run(path, audit, out):
    return report.main(["--plan-doc", str(path), "--audit-log", str(audit), "--output", str(out)])


def last_row(audit):
    return parse_audit_line(audit.read_text().strip().splitlines()[-1])


def test_success_run_writes_tables_slots_and_report_line(tmp_path, capsys):
    path, audit = make_plan(tmp_path)
    audit_append(str(audit), "DRAIN-P2", "START", "pid=1")
    finish(path, audit, "cp")
    phase(audit, 2, "PASS")
    audit_append(str(audit), "DRAIN-P2", "WARN", "FailedDrain: node doesn't drain | pdb")
    phase(audit, 3, "PASS")
    finish(path, audit, "full")
    phase(audit, 6, "PASS")
    audit_append(str(audit), "PHASE7", "ENTER", "")
    phase(audit, 4, "FAIL")  # Phase 7 nested sub-gate copy: not a Phase 4 result of this run
    audit_append(str(audit), "PHASE7", "PASS", "exit=0")
    out = tmp_path / "r.md"
    assert run(path, audit, out) == 0
    text = out.read_text()
    assert "✅ 성공" in text and "| Phase 0 |" in text and "| Phase 4 |" not in text
    assert "| cp | cp | 완료 |" in text and "| full | full | 완료 |" in text
    assert "cp.tfplan" in text and "doesn't drain \\| pdb" in text
    assert text.count("<!-- NARRATIVE:") == 4
    row = last_row(audit)
    assert (row["rule_id"], row["result"]) == ("REPORT", "PASS")
    assert up.fields(row["detail"])["result"] == "success"
    assert "result=success" in capsys.readouterr().out


def test_cumulative_log_from_earlier_run_does_not_leak(tmp_path):
    audit = tmp_path / "audit.log"
    # An earlier run on another target: its approval, phases and drain events precede ours.
    seed_preflight(audit, target="1.34", phase0="FAIL")
    audit_append(str(audit), "PLAN-APPROVED", "PASS", up.detail_fields(plan_doc=str(tmp_path / "old.json"), sha256="x", answer="승인"))
    phase(audit, 2, "FAIL")
    audit_append(str(audit), "DRAIN-P4", "FAIL", "old-run-event")
    path, audit = make_plan(tmp_path)
    phase(audit, 2, "PASS")
    out = tmp_path / "r.md"
    assert run(path, audit, out) == 0
    text = out.read_text()
    assert "old-run-event" not in text and "| FAIL |" not in text and "| Phase 2 | " in text
    assert "| PASS | 0 | 0 | 0 | 0 | 0 |" in text  # Phase 0 is this target's gate_check block
    assert up.fields(last_row(audit)["detail"])["result"] == "incomplete"


def test_failed_run_reports_failing_phase_in_english(tmp_path):
    path, audit = make_plan(tmp_path, pools=("MNG",), language="en")
    finish(path, audit, "cp")
    phase(audit, 2, "PASS")
    phase(audit, 3, "PASS")
    phase(audit, 4, "FAIL")
    out = tmp_path / "r.md"
    assert run(path, audit, out) == 0
    text = out.read_text()
    assert "❌ Failed at Phase 4" in text and "| nodes | nodes | not reached |" in text
    meta = up.fields(last_row(audit)["detail"])
    assert (meta["result"], meta["phase"]) == ("failed", "4")


def test_unapproved_or_missing_inputs_stop(tmp_path, capsys):
    path, audit = make_plan(tmp_path, approve=False)
    before = audit.read_text()
    assert run(path, audit, tmp_path / "r.md") == 1
    assert run(tmp_path / "missing.json", audit, tmp_path / "r.md") == 1
    assert "NEXT: STOP" in capsys.readouterr().out
    assert audit.read_text() == before and not (tmp_path / "r.md").exists()


def test_changed_plan_after_approval_stops(tmp_path):
    path, audit = make_plan(tmp_path)
    path.write_text(path.read_text().replace('"demo"', '"other"'))
    assert run(path, audit, tmp_path / "r.md") == 1


def test_forged_report_event_is_not_needed_and_default_name(tmp_path, monkeypatch):
    path, audit = make_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert report.main(["--plan-doc", str(path), "--audit-log", str(audit)]) == 0
    assert len(list(tmp_path.glob("upgrade-report-demo-*.md"))) == 1


def test_free_text_apostrophe_detail_does_not_break_plan_progression(tmp_path):
    path, audit = make_plan(tmp_path)
    audit_append(str(audit), "DRAIN-P2", "WARN", "pod doesn't evict")
    assert up.main(["next", "--plan-doc", str(path), "--audit-log", str(audit)]) == 0
    assert up.fields("pod doesn't evict") == {}


def test_failed_run_gets_failed_name_and_reports_never_overwrite(tmp_path, monkeypatch):
    path, audit = make_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    phase(audit, 2, "FAIL")
    assert report.main(["--plan-doc", str(path), "--audit-log", str(audit)]) == 0
    first = next(tmp_path.glob("upgrade-report-demo-*-FAILED.md"))
    first.write_text("filled narrative")
    assert report.main(["--plan-doc", str(path), "--audit-log", str(audit)]) == 0
    assert first.read_text() == "filled narrative"
    second = first.with_name(first.stem + "-2.md")
    assert "<!-- NARRATIVE:summary" in second.read_text()
    assert up.fields(last_row(audit)["detail"])["output"] == str(second)
