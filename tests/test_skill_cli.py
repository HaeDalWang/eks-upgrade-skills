"""Validate documented script commands through the scripts' real argparse parsers."""

import argparse
from pathlib import Path
import re
import runpy
import shlex
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "k8s-upgrade-skills/aws/terraform-eks/SKILL.md"
SCRIPTS = ROOT / "k8s-upgrade-skills/scripts"


class ParsedCommand(Exception):
    """Stop immediately after argparse, before any command can touch infrastructure."""


def script_commands():
    text = SKILL.read_text().replace("\\\n", " ")
    return [shlex.split(match.group(1).removesuffix(" &&")) for match in re.finditer(
        r"^\s*python3\s+(scripts/[\w]+\.py[^\n]*)", text, re.MULTILINE
    )]


def parse_without_execution(monkeypatch, command):
    original_parse_args = argparse.ArgumentParser.parse_args

    def parse_args(parser, args=None, namespace=None):
        original_parse_args(parser, args, namespace)
        raise ParsedCommand

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_args)
    monkeypatch.setattr(sys, "argv", command)
    with pytest.raises(ParsedCommand):
        runpy.run_path(str(SCRIPTS / Path(command[0]).name), run_name="__main__")


@pytest.mark.parametrize("command", script_commands(), ids=lambda args: " ".join(args[:2]))
def test_every_documented_script_invocation_uses_accepted_arguments(monkeypatch, command):
    parse_without_execution(monkeypatch, command)


def test_skill_command_extraction_includes_every_script(monkeypatch):
    commands = script_commands()
    assert commands
    assert {Path(args[0]).name for args in commands} == {
        "gate_check.py", "phase_gate.py", "drain_watch.py", "service_watch.py",
        "audit_event.py", "plan_check.py", "apply_guard.py",
    }


def test_phase3_target_version_is_rejected_before_execution(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        parse_without_execution(monkeypatch, [
            "scripts/phase_gate.py", "phase3", "--cluster-name", "test",
            "--target-version", "1.34",
        ])
    assert exc.value.code == 1
