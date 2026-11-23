"""Offline lint of the skill docs: gate, monitor and guard order must survive any doc edit."""
import re
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1] / "k8s-upgrade-skills"
SKILL = SKILL_ROOT / "SKILL.md"
STEPS = sorted((SKILL_ROOT / "steps").glob("*.md"))
OWNER = {2: "cp.md", 3: "addons.md", 4: "nodes.md", 5: "karpenter.md", 6: "full.md", 7: "final.md"}
FENCE = re.compile(r"^\s*```(\w*)\n(.*?)^\s*```", re.MULTILINE | re.DOTALL)


def commands(path):
    """Shell lines inside bash fences, backslash continuations joined, in document order."""
    lines = []
    for match in FENCE.finditer(path.read_text()):
        if match.group(1) != "bash":
            continue
        for line in match.group(2).replace("\\\n", " ").splitlines():
            if line.strip():
                lines.append(line.strip())
    return lines


def planfile(line):
    found = re.search(r"/([\w-]+)\.tfplan", line)
    return found.group(1) if found else None


def test_skill_md_stays_short():
    assert len(SKILL.read_text().splitlines()) <= 170


def test_expected_step_files_exist():
    assert {p.name for p in STEPS} == {"start.md", "cp.md", "addons.md", "nodes.md", "karpenter.md",
                                       "full.md", "final.md", "failure.md"}


@pytest.mark.parametrize("phase", sorted(OWNER))
def test_each_phase_gate_is_invoked_only_in_its_step_file(phase):
    gate = re.compile(r"^python3 scripts/phase_gate\.py phase{}\b".format(phase))
    owners = [p.name for p in [SKILL, *STEPS] if any(gate.match(c) for c in commands(p))]
    assert owners == [OWNER[phase]]


def test_step_loop_table_keeps_phase_order():
    order = re.findall(r"^\| \[steps/(\w+\.md)\]", SKILL.read_text(), re.MULTILINE)
    assert order == [OWNER[n] for n in sorted(OWNER)]


@pytest.mark.parametrize("name", ["nodes.md", "karpenter.md"])
def test_phase4_and_5_gates_run_without_their_plan_step(name):
    text = (SKILL_ROOT / "steps" / name).read_text()
    assert re.search(r"^## \d-\d\. Phase \d gate \(always\)$", text, re.MULTILINE)
    assert "always runs" in text.split("\n## ")[0]  # the preamble says so before any section


@pytest.mark.parametrize("path", STEPS, ids=lambda p: p.name)
def test_every_step_file_starts_with_upgrade_plan_next(path):
    text = path.read_text()
    first_fence = FENCE.search(text)
    assert first_fence and "\n## " not in text[:first_fence.start()]
    assert commands(path)[0].startswith("python3 scripts/upgrade_plan.py next ")


@pytest.mark.parametrize("path", STEPS, ids=lambda p: p.name)
def test_monitor_plan_check_and_guard_precede_every_apply(path):
    cmds = commands(path)
    window = []  # commands since the previous apply
    for index, line in enumerate(cmds):
        if "terraform apply" not in line:
            window.append(line)
            continue
        assert any(c.startswith("python3 scripts/drain_watch.py ") for c in cmds[:index]), line
        checks = [c for c in window if c.startswith("python3 scripts/plan_check.py ")]
        guards = [c for c in window if c.startswith("python3 scripts/apply_guard.py ")]
        assert checks and guards, line
        assert window.index(checks[-1]) < window.index(guards[-1])
        assert planfile(checks[-1]) == planfile(guards[-1]) == planfile(line), line
        window = []


@pytest.mark.parametrize("path", [SKILL, *STEPS], ids=lambda p: p.name)
def test_no_auto_approve_or_removed_flags_in_commands(path):
    for line in commands(path):
        assert "-auto-approve" not in line
        assert not re.search(r"--step(?![-\w])|--expect-ami-var", line), line


@pytest.mark.parametrize("phase,step", [(2, "cp"), (4, "nodes"), (5, "karpenter"), (6, "full")])
def test_step_done_follows_its_gate(phase, step):
    cmds = commands(SKILL_ROOT / "steps" / OWNER[phase])
    gate = next(i for i, c in enumerate(cmds) if c.startswith("python3 scripts/phase_gate.py phase{}".format(phase)))
    done = next(i for i, c in enumerate(cmds) if "upgrade_plan.py done" in c and "--step-id " + step in c)
    assert gate < done


@pytest.mark.parametrize("name", ["final.md", "failure.md"])
def test_report_is_written_by_script(name):
    cmds = commands(SKILL_ROOT / "steps" / name)
    report = [i for i, c in enumerate(cmds) if c.startswith("python3 scripts/report.py ")]
    assert report
    if name == "final.md":
        gate = next(i for i, c in enumerate(cmds) if c.startswith("python3 scripts/phase_gate.py phase7"))
        assert gate < report[0]
