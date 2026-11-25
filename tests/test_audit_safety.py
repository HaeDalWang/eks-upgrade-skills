"""Shared audit format and block-summary regressions."""
import lib


def test_phase_fail_summary_is_blocked_instead_of_open_zero_of_zero(tmp_path):
    lib.reset_gate()
    lib.audit_init("test", "", "1.34")
    lib.audit_write("PHASE2-CP", "FAIL", "status=UPDATING")
    path = tmp_path / "audit.log"
    lib.audit_flush(str(path))
    content = path.read_text()
    assert "# Gate: BLOCKED" in content
    assert "PASS=0/1" in content


import pytest
from unittest.mock import patch
import subprocess
import phase_gate


@pytest.mark.parametrize("result", ["PASS", "WARN", "FAIL", "INFO"])
def test_parser_reads_old_four_column_records_including_detail_pipes(result):
    row = lib.parse_audit_line("2026-01-01T00:00:00Z | RULE | " + result + " | text | extra")
    assert row["severity"] == "-"
    assert row["result"] == result
    assert row["detail"] == "text | extra"


def test_severity_and_skipped_rules_in_block_summary(tmp_path):
    lib.reset_gate()
    lib.audit_init("test", "1.33", "1.34")
    lib.record("COM-001", "CRITICAL", "PASS", "ok")
    lib.record("WLS-002", "HIGH", "FAIL", "needs review")
    lib.record("INF-001", "LOW", "SKIP", "not configured")
    log = tmp_path / "audit.log"
    lib.audit_flush(str(log))
    content = log.read_text()
    assert "# Gate: WARN" in content
    assert "PASS=1/2" in content
    assert "# Executed rules: 2" in content
    rows = [row for line in content.splitlines() if (row := lib.parse_audit_line(line))]
    assert rows[1]["severity"] == "HIGH"
    assert "# Skill commit:" in content


def test_audit_append_does_not_inject_lines(tmp_path):
    log = tmp_path / "audit.log"
    lib.audit_append(str(log), "TEST", "WARN", "hello\nforged")
    assert len(log.read_text().splitlines()) == 1
    assert lib.parse_audit_line(log.read_text())["detail"] == r"hello\nforged"


@pytest.mark.parametrize("code,rows,action", [
    (1, [], "STOP"),
    (2, [{"rule_id": "PHASE3-ADDON", "result": "WARN", "detail": "UPDATING"}], "RETRY"),
    (2, [{"rule_id": "PHASE4-DATAPLANE", "result": "WARN", "detail": "TRANSIENT Pod 1개: default/a"}], "RETRY"),
    (2, [{"rule_id": "PHASE4-DATAPLANE", "result": "WARN", "detail": "STALE Pod 2개: default/a, ns/b — 삭제 필요"}], "RUN delete-stale-pods default/a ns/b"),
    (2, [], "ASK_USER"),
])
def test_gate_next_routes_every_existing_action(code, rows, action):
    assert phase_gate.gate_next(code, rows).startswith("NEXT: " + action)


def test_phase_boundary_fail_records_enter_result_and_final_next(tmp_path, capsys):
    log = tmp_path / "audit.log"
    with patch("phase_gate.run_cmd", return_value=subprocess.CompletedProcess([], 1, "", "denied")):
        assert phase_gate.gate_phase2("test", "1.34", str(log)) == 1
    rows = [row for line in log.read_text().splitlines() if (row := lib.parse_audit_line(line))]
    assert (rows[0]["rule_id"], rows[0]["result"]) == ("PHASE2", "ENTER")
    assert (rows[-1]["rule_id"], rows[-1]["result"]) == ("PHASE2", "FAIL")
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: STOP")


def test_phase_boundary_warn_records_final_retry(tmp_path, capsys):
    @phase_gate.phase_boundary(3)
    def warn_gate(audit_log):
        lib.reset_gate()
        lib.audit_init("test", "", "")
        lib.audit_write("PHASE3-ADDON", "WARN", "UPDATING")
        lib.audit_flush(audit_log)
        return 2
    assert warn_gate(str(tmp_path / "audit.log")) == 2
    assert capsys.readouterr().out.splitlines()[-1].startswith("NEXT: RETRY")


@pytest.mark.parametrize("marker,expected", [(None, "unknown"), ("v2.0.0", "v2.0.0")])
def test_skill_revision_uses_install_marker_when_skill_is_not_a_repo(tmp_path, monkeypatch, marker, expected):
    scripts = tmp_path / "skill" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(lib, "__file__", str(scripts / "lib.py"))
    monkeypatch.setattr(lib, "run_cmd", lambda command: subprocess.CompletedProcess(command, 1, "", "no repo"))
    if marker:
        (scripts.parent / "VERSION").write_text(marker)
    assert lib.skill_revision() == expected


def test_skill_revision_asks_git_in_skill_directory(monkeypatch):
    seen = []
    def git(command):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "abcdef0\n", "")
    monkeypatch.setattr(lib, "run_cmd", git)
    assert lib.skill_revision() == "abcdef0"
    from pathlib import Path
    assert seen == [["git", "-C", str(Path(lib.__file__).resolve().parent.parent),
                     "rev-parse", "--short", "HEAD"]]


@pytest.mark.parametrize("script", ["gate_check.py", "phase_gate.py", "plan_check.py", "apply_guard.py"])
def test_invalid_gate_arguments_exit_one_with_final_next_stop(script):
    import sys
    result = subprocess.run([sys.executable, "eks-upgrade-skills/scripts/" + script],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1].startswith("NEXT: STOP ")


def test_usage_is_written_before_final_stdout_stop(capsys):
    from lib import GateArgumentParser
    parser = GateArgumentParser(prog="gate")
    parser.add_argument("--required", required=True)
    events = []
    original_usage = parser.print_usage
    def usage(file=None):
        assert not capsys.readouterr().out
        events.append("usage")
        original_usage(file)
    parser.print_usage = usage
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert events == ["usage"]
    assert captured.out.splitlines()[-1].startswith("NEXT: STOP ")
