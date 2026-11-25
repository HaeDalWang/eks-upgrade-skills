"""Exercise the actual hook stdin/stdout contract without Terraform or a model."""
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys

import pytest
from upgrade_plan import detail_fields

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'hooks/apply_guard_hook.py'


def row(rule, result='PASS', **metadata):
    return '2026-11-24T00:00:00Z | {} | - | {} | {}\n'.format(
        rule, result, detail_fields(**metadata))


def approve(project, **metadata):
    with (project / 'audit.log').open('a') as stream:
        stream.write(row('PLAN-APPROVED', **metadata))


def guard(project, plan, result='PASS', **metadata):
    with (project / 'audit.log').open('a') as stream:
        stream.write(row('APPLY-GUARD', result, planfile=str(plan.resolve()),
                         sha256=hashlib.sha256(plan.read_bytes()).hexdigest(), **metadata))


def invoke(project, command='terraform apply cp.tfplan', raw=None, tool='Bash'):
    payload = json.dumps({'cwd': str(project), 'tool_name': tool,
                          'tool_input': {'command': command}}) if raw is None else raw
    result = subprocess.run([sys.executable, str(HOOK)], input=payload, cwd=project,
                            env={**os.environ, 'CLAUDE_PLUGIN_ROOT': str(ROOT)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and not result.stderr, result.stderr
    if not result.stdout:
        return None
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse' and output['permissionDecision'] == 'deny'
    reason = output['permissionDecisionReason']
    assert 'python3 scripts/apply_guard.py --planfile ' in reason and '\n' not in reason
    return reason


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'cp.tfplan').write_bytes(b'saved plan')
    return tmp_path


def test_inactive_run_allows_apply_and_malformed_input(project):
    assert invoke(project) is None
    assert invoke(project, raw='{') is None
    (project / 'audit.log').write_text(row('PHASE0', 'PASS'))
    assert invoke(project) is None


def test_matching_pass_allows_unchanged_plan(project):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project) is None


@pytest.mark.parametrize('problem', ['missing', 'last-fail', 'changed', 'other-file'])
def test_missing_or_stale_guard_denies(project, problem):
    approve(project)
    if problem != 'missing':
        plan = project / ('other.plan' if problem == 'other-file' else 'cp.tfplan')
        plan.write_bytes(b'saved plan')
        guard(project, plan)
        if problem == 'last-fail':
            guard(project, plan, 'FAIL')
        if problem == 'changed':
            plan.write_bytes(b'changed plan')
    assert invoke(project)


@pytest.mark.parametrize('args', ['-auto-approve cp.tfplan', '-auto-approve=false cp.tfplan', '',
                                '-destroy cp.tfplan', '-target=module.eks cp.tfplan',
                                '-replace=x cp.tfplan', '-var=x=y cp.tfplan',
                                '-var-file vars cp.tfplan'])
def test_apply_requires_saved_plan_without_planning_flags(project, args):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, 'terraform apply ' + args)


@pytest.mark.parametrize('command', [
    'cd x && terraform -chdir=y apply p.tfplan',
    'sh -c "cd x && terraform -chdir=y apply p.tfplan"',
    'aws-vault exec p -- terraform -chdir=x/y apply p.tfplan',
    'true; terraform -chdir=x/y apply p.tfplan | cat',
    'true\nterraform -chdir=x/y apply p.tfplan',
])
def test_shell_segments_auth_prefix_and_chdir_resolve_plan(project, command):
    approve(project)
    directory = project / 'x/y'
    directory.mkdir(parents=True)
    plan = directory / 'p.tfplan'
    plan.write_bytes(b'saved plan')
    assert invoke(project, command)
    guard(project, plan)
    assert invoke(project, command) is None


@pytest.mark.parametrize('command', ['terraform apply "$PLAN"', 'terraform apply cp.tfplan "',
                                    'cd x; terraform apply cp.tfplan',
                                    '$(echo terraform) apply cp.tfplan'])
def test_ambiguous_apply_denies_in_active_run(project, command):
    approve(project)
    assert invoke(project, command)


@pytest.mark.parametrize('boundary', ['approval', 'target', 'done', 'skip'])
def test_cumulative_logs_cannot_reuse_previous_execution_or_target(project, boundary):
    approve(project, target_version='1.34')
    guard(project, project / 'cp.tfplan', target_version='1.34')
    if boundary in ('approval', 'target'):
        approve(project, target_version='1.35' if boundary == 'target' else '1.34')
    else:
        with (project / 'audit.log').open('a') as stream:
            stream.write(row('STEP-DONE' if boundary == 'done' else 'DEVIATION',
                             'PASS' if boundary == 'done' else 'SKIP'))
    assert invoke(project)
    guard(project, project / 'cp.tfplan', target_version='1.35' if boundary == 'target' else '1.34')
    assert invoke(project) is None


def test_wrong_target_guard_after_current_approval_denies(project):
    approve(project, target_version='1.35')
    guard(project, project / 'cp.tfplan', target_version='1.34')
    assert invoke(project)


def test_unrelated_commands_and_tools_allow(project):
    approve(project)
    assert invoke(project, 'git status') is None
    assert invoke(project, tool='Read') is None
    assert invoke(project, 'terraform plan -out=cp.tfplan') is None


def test_malformed_stdin_while_active_denies(project):
    approve(project)
    assert invoke(project, raw='{')
    assert invoke(project, raw='[]')


def test_audit_event_refuses_forged_guard(project):
    approve(project)
    script = ROOT / 'eks-upgrade-skills/scripts/audit_event.py'
    result = subprocess.run([sys.executable, str(script), '--audit-log', str(project / 'audit.log'),
                             '--rule-id', 'APPLY-GUARD', '--result', 'PASS', '--detail', 'forged'],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert invoke(project)


@pytest.mark.parametrize('command', ["terra'form' apply cp.tfplan", r'terrafor\m apply cp.tfplan',
                                    "sh -c \"terra'form' apply cp.tfplan\"",
                                    'terra\\\nform apply cp.tfplan'])
def test_shell_quoted_or_escaped_terraform_is_checked(project, command):
    approve(project)
    assert invoke(project, command)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, command) is None


def test_every_apply_in_a_command_requires_its_own_guard(project):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, 'terraform apply cp.tfplan && terraform apply unchecked.tfplan')


def test_pass_for_another_plan_does_not_override_valid_matching_pass(project):
    approve(project)
    guard(project, project / 'cp.tfplan')
    other = project / 'other.tfplan'
    other.write_bytes(b'other')
    guard(project, other)
    assert invoke(project) is None


def test_real_guard_record_binds_approval_and_provides_complete_next_command(project):
    import apply_guard
    from test_apply_guard import setup
    saved, audit, metadata, args = setup(project)
    command = 'terraform apply ' + shlex.quote(str(saved))
    reason = invoke(project, command)
    assert '--cluster-name demo --target-version 1.35' in reason
    assert '--plan-doc ' + args.plan_doc in reason
    assert apply_guard.main(['--planfile', str(saved), '--plan-doc', args.plan_doc,
                             '--cluster-name', 'demo', '--target-version', '1.35',
                             '--audit-log', str(audit)]) == 0
    assert invoke(project, command) is None
    plan_doc = Path(args.plan_doc)
    plan_doc.write_text(plan_doc.read_text() + '\n')
    assert 'approved plan JSON changed' in invoke(project, command)


@pytest.mark.parametrize('command', ['terraform apply cp.tfplan || cd x && terraform apply p.tfplan',
                                    'if false; then cd x && true; fi; terraform apply p.tfplan',
                                    'cd x && false; terraform apply p.tfplan'])
def test_conditional_cwd_cannot_authorize_a_different_plan(project, command):
    approve(project)
    directory = project / 'x'
    directory.mkdir()
    plan = directory / 'p.tfplan'
    plan.write_bytes(b'other plan')
    guard(project, plan)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, command)


@pytest.mark.parametrize('command', ['terraform -chdir=x -chdir=y apply p.tfplan',
                                    'env -C x terraform apply cp.tfplan',
                                    'sudo --chdir=x terraform apply cp.tfplan'])
def test_ambiguous_wrapper_or_repeated_chdir_denies(project, command):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, command)


def documented_applies():
    from test_skill_docs import STEPS, commands
    return [(path.name, line) for path in STEPS for line in commands(path)
            if 'terraform apply' in line]


@pytest.mark.parametrize('name,line', documented_applies())
def test_documented_apply_is_literal_and_requires_matching_guard(project, name, line):
    assert '${' not in line and '(' not in line, (name, line)
    command = line.replace('<TF_DIR>', str(project)).replace('<AUTH_PREFIX>', 'aws-vault exec demo --')
    plan = project / shlex.split(command)[-1]
    plan.write_bytes(b'documented saved plan')
    approve(project)
    assert invoke(project, command)
    guard(project, plan)
    assert invoke(project, command) is None


def test_raw_variable_apply_has_literal_path_hint(project):
    approve(project)
    reason = invoke(project, '(cd "${TF_DIR}" && ${AUTH_PREFIX} terraform apply "${TF_DIR}/cp.tfplan")')
    assert 'write the apply with literal paths, no $VAR or ( )' in reason


@pytest.mark.parametrize('shell', ['bash -lc', 'sh -ec', 'zsh -cl'])
def test_nested_combined_shell_flags_cannot_hide_unchecked_apply(project, shell):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, shell + " 'terraform apply other.tfplan'")
    assert invoke(project, 'terraform apply cp.tfplan && ' + shell + " 'terraform apply other.tfplan'")


def test_child_cwd_uses_parent_audit_and_real_guard_paths(project):
    import apply_guard
    from test_apply_guard import setup
    saved, audit, metadata, args = setup(project)
    child = project / 'terraform'
    child.mkdir()
    command = 'terraform apply ' + shlex.quote(str(saved))
    assert invoke(child, command)
    assert apply_guard.main(['--planfile', str(saved), '--plan-doc', args.plan_doc,
                             '--cluster-name', 'demo', '--target-version', '1.35',
                             '--audit-log', str(audit)]) == 0
    assert invoke(child, command) is None


@pytest.mark.parametrize('prefix', ['builtin cd x', 'command cd x', 'true && cd x'])
def test_nonleading_or_wrapped_cd_cannot_use_wrong_plan_guard(project, prefix):
    approve(project)
    guard(project, project / 'cp.tfplan')
    (project / 'x').mkdir()
    assert invoke(project, prefix + ' && terraform apply cp.tfplan')


def test_unresolved_raw_apply_cannot_hide_behind_resolved_apply(project):
    approve(project)
    guard(project, project / 'cp.tfplan')
    assert invoke(project, "terraform apply cp.tfplan && dash -c 'terraform apply other.tfplan'")


def test_combined_shell_flags_allow_only_after_both_guards(project):
    approve(project)
    guard(project, project / 'cp.tfplan')
    other = project / 'other.tfplan'
    other.write_bytes(b'another plan')
    command = "terraform apply cp.tfplan && bash -lc 'terraform apply other.tfplan'"
    assert invoke(project, command)
    guard(project, other)
    assert invoke(project, command) is None


def test_documented_apply_is_a_separate_bash_call_from_guard():
    from test_skill_docs import FENCE, STEPS
    for path in STEPS:
        for fence in FENCE.finditer(path.read_text()):
            if fence.group(1) == 'bash' and 'terraform apply' in fence.group(2):
                assert 'apply_guard.py' not in fence.group(2), path.name
