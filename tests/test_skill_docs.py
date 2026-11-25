"""Offline lint of the skill docs: gate, monitor and guard order must survive any doc edit."""
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1] / "eks-upgrade-skills"
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
                                       "full.md", "final.md", "failure.md", "user-apply.md"}


@pytest.mark.parametrize("phase", sorted(OWNER))
def test_each_phase_gate_is_invoked_only_in_its_step_file(phase):
    gate = re.compile(r"^python3 scripts/phase_gate\.py phase{}\b".format(phase))
    owners = [p.name for p in [SKILL, *STEPS] if any(gate.match(c) for c in commands(p))]
    assert owners == [OWNER[phase]]


DISPATCH = {"cp": "cp.md", "addon": "addons.md", "nodes": "nodes.md", "karpenter": "karpenter.md",
            "full": "full.md", "null": "final.md"}
GATE_ONLY = [("addons.md", "3-1, 3-2", "addon", ("nodes", "karpenter", "full")),
             ("nodes.md", "4-0, 4-0b, 4-6", "nodes", ("karpenter", "full")),
             ("karpenter.md", "5-2", "karpenter", ("full",))]


def dispatch_table():
    return dict(re.findall(r"^\| `(\w+)` \| \[steps/(\w+\.md)\]", SKILL.read_text(), re.MULTILINE))


def gate_only_table():
    rows = re.findall(r"^\| steps/(\w+\.md) \(([^)]*)\) \| `(\w+)` step[^|]*\| ([^|]*) \|$",
                      SKILL.read_text(), re.MULTILINE)
    return [(f, sections, missing, tuple(re.findall(r"`(\w+)`", triggers)))
            for f, sections, missing, triggers in rows]


def walk(order):
    """Files the SKILL.md step loop opens for an approved order: dispatch by type plus gate-only runs."""
    table, files, settled = dispatch_table(), [], set()
    after_cp = order[order.index("cp") + 1:order.index("cp") + 2]
    for kind in order:
        for name, _, missing, triggers in gate_only_table():
            if name in settled or kind not in triggers:
                continue
            settled.add(name)
            if (after_cp != ["addon"]) if missing == "addon" else (missing not in order):
                files.append(name)
        files.append(table[kind])
    return files + [table["null"]]


def test_step_loop_dispatches_every_plan_step_type_to_its_file():
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    from plan_check import STEPS as TYPES
    assert dispatch_table() == DISPATCH
    assert set(TYPES) | {"null"} == set(DISPATCH)


def test_gate_only_runs_are_defined_once_in_skill_md():
    assert gate_only_table() == GATE_ONLY
    assert [p.name for p in STEPS if "| steps/" in p.read_text()] == []


@pytest.mark.parametrize("pools", [(), ("MNG",), ("Karpenter",), ("MNG", "Karpenter"), ("Fargate",)])
def test_default_orders_walk_the_old_fixed_file_order(pools):
    order = ["cp"] + (["nodes"] if "MNG" in pools else []) + (
        ["karpenter"] if "Karpenter" in pools else []) + ["full"]
    assert walk(order) == [OWNER[n] for n in sorted(OWNER)]


@pytest.mark.parametrize("order", [["cp", "karpenter", "nodes", "full"], ["cp", "addon", "nodes", "full"],
                                   ["cp", "nodes", "addon", "full"], ["cp", "full", "karpenter"]])
def test_custom_orders_run_every_step_in_order_and_every_gate(order):
    files = walk(order)
    assert files[:2] == ["cp.md", "addons.md"]  # Phase 3 right after the cp step's Phase 2 gate
    assert set(files) == set(OWNER.values()) and files[-1] == "final.md"
    rest = iter(files)
    assert all(DISPATCH[kind] in rest for kind in order)  # every approved step, in approved order


@pytest.mark.parametrize("name,gate", [("addons.md", "3-2"), ("nodes.md", "4-6"), ("karpenter.md", "5-2")])
def test_phase3_4_5_gates_sit_outside_every_step_type_condition(name, gate):
    text = (SKILL_ROOT / "steps" / name).read_text()
    preamble = text.split("\n## ")[0]
    assert "always runs" in preamble
    phase, number = gate.split("-")
    for major, first, last in re.findall(r"skip (\d)-(\d+)(?: → \d-(\d+))?", preamble):
        assert not (major == phase and int(first) <= int(number) <= int(last or first))
    section = text.split("\n## {}. ".format(gate))[1]
    assert section.startswith("Phase {} gate (always)\n".format(phase))
    prose = section.split("python3 scripts/phase_gate.py phase" + phase)[0].split("\n", 1)[1]
    assert not re.search(r"\bonly\b|\bif\b|\btype\b|\bskip\b", prose, re.IGNORECASE)


@pytest.mark.parametrize("name,phase", [("nodes.md", "P4"), ("karpenter.md", "P5")])
def test_service_watch_starts_before_node_changes_when_services_exist(name, phase):
    text = (SKILL_ROOT / "steps" / name).read_text()
    assert re.search(r"^## \d-0b\. Start the service monitor \(only when the recipe has `services`\)$",
                     text, re.MULTILINE)
    cmds = commands(SKILL_ROOT / "steps" / name)
    watch = next(i for i, c in enumerate(cmds) if c.startswith("python3 scripts/service_watch.py --phase " + phase))
    assert watch < next(i for i, c in enumerate(cmds) if "terraform apply" in c)


VAR_BLOCK = re.compile(r"```bash\neval \"\$\(python3 - <<'PY'\n(.*?)\nPY\n\)\"\n```", re.DOTALL)
DISCOVERY_VARS = {"CLUSTER_VERSION_VAR", "CLUSTER_VERSION_FILE", "MNG_AMI_VAR", "MNG_AMI_FILE",
                  "KARPENTER_AMI_VAR", "KARPENTER_AMI_FILE", "EKS_MODULE", "CLUSTER_NAME", "TF_DIR"}


@pytest.mark.parametrize("path", [p for p in STEPS if p.name not in ("start.md", "failure.md", "user-apply.md")],
                         ids=lambda p: p.name)
def test_step_files_rederive_discovery_variables_before_first_section(path):
    text = path.read_text()
    block = VAR_BLOCK.search(text)
    assert set(re.findall(r"\$\{(\w+)\}", text)) & DISCOVERY_VARS
    assert block and block.start() < text.index("\n## ")
    assert block.group(1) == VAR_BLOCK.search((SKILL_ROOT / "steps/cp.md").read_text()).group(1)


def test_variable_block_prints_shell_assignments_from_discovery(tmp_path):
    (tmp_path / "discovery.json").write_text(json.dumps({
        "cluster_name": "demo", "target_version": "1.35", "tf_dir": "/w/tf it's",
        "module": {"address": "module.eks"},
        "cluster_version": {"variable": "eks_cluster_version", "set_in": "prod.tfvars"},
        "mng": None, "karpenter": {"alias_variable": "alias", "alias_variable_set_in": "default"}}))
    code = VAR_BLOCK.search((SKILL_ROOT / "steps/cp.md").read_text()).group(1)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         check=True).stdout
    values = dict(shlex.split(line)[0].split("=", 1) for line in out.splitlines())
    assert values["TF_DIR"] == "/w/tf it's" and values["EKS_MODULE"] == "module.eks"
    assert values["CLUSTER_VERSION_FILE"] == "prod.tfvars" and values["MNG_AMI_VAR"] == ""
    assert values["KARPENTER_AMI_FILE"] == "default" and values["TARGET_VERSION"] == "1.35"


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


@pytest.mark.parametrize("phase,step", [(2, "cp"), (3, "addon"), (4, "nodes"), (5, "karpenter"), (6, "full")])
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


def test_plugin_manifest_paths_and_hook_command_exist():
    root = SKILL_ROOT.parent
    plugin = json.loads((root / '.claude-plugin/plugin.json').read_text())
    marketplace = json.loads((root / '.claude-plugin/marketplace.json').read_text())
    hooks = json.loads((root / 'hooks/hooks.json').read_text())
    assert plugin['name'] == marketplace['name'] == 'eks-upgrade-skills'
    assert plugin['skills'] == ['./eks-upgrade-skills', './helm-k8s-compat']
    assert all((root / path / 'SKILL.md').is_file() for path in plugin['skills'])
    assert marketplace['plugins'] == [{'name': plugin['name'], 'source': './'}]
    entry = hooks['hooks']['PreToolUse'][0]
    assert entry['matcher'] == 'Bash'
    command = entry['hooks'][0]['command'].replace('${CLAUDE_PLUGIN_ROOT}', str(root))
    executable, path = shlex.split(command)
    assert executable == 'python3' and Path(path).is_file()


def test_user_apply_preserves_guard_wait_confirm_order_at_every_site():
    text = (SKILL_ROOT / "steps/user-apply.md").read_text()
    assert text.index("`plan_check.py`") < text.index("`apply_guard.py`") < text.index("2. Show")
    assert text.index("Wait for the user's reply") < text.index("python3 scripts/apply_confirm.py")
    assert '그냥 너가 해' in text and 'phase gate' in text
    for name in ("cp", "addons", "nodes", "karpenter", "full"):
        text = (SKILL_ROOT / "steps" / (name + ".md")).read_text()
        assert text.count('Call 2:') == text.count('[user-apply.md](user-apply.md)')
