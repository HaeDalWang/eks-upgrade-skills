"""Fast real-script integration checks against fake CLIs; no Claude or credentials."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from evals.grading import grade, read_jsonl
from evals.runner import (INVALID, REPO, SCENARIO, accounting, answer, command,
                          guard, invoke, run_live)


def cli(sandbox, tool, *args, check=True):
    run, env, _ = sandbox
    result = subprocess.run([str(run / 'bin' / tool), *args], cwd=run / 'project',
                            env=env, capture_output=True, text=True, timeout=15)
    if check:
        assert result.returncode == 0, result.stderr + result.stdout
    return result


def script(sandbox, name, *args):
    run, env, _ = sandbox
    root = run / 'home/.claude/skills/k8s-upgrade-skills/scripts'
    return subprocess.run([str(run / 'bin/python3'), str(root / name), *args],
                          cwd=run / 'project', env=env, capture_output=True,
                          text=True, timeout=60)


def update_tfvars(run, old, new):
    path = run / 'project/terraform.tfvars'
    path.write_text(path.read_text().replace(old, new))


def test_guard_refuses_real_binary_first(sandbox, tmp_path):
    run, env, _ = sandbox
    real = tmp_path / 'real-bin'
    real.mkdir()
    binary = real / 'kubectl'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    with pytest.raises(ValueError, match='kubectl must resolve'):
        guard({**env, 'PATH': str(real) + ':' + env['PATH']}, run / 'bin')


@pytest.mark.parametrize('key', ['AWS_PROFILE', 'AWS_ACCESS_KEY_ID', 'KUBECONFIG'])
def test_guard_refuses_credential_environment(sandbox, key):
    run, env, _ = sandbox
    with pytest.raises(ValueError, match='forbidden environment'):
        guard({**env, key: 'do-not-use'}, run / 'bin')


def test_sandbox_installs_unchanged_skill_and_clean_environment(sandbox):
    run, env, scenario = sandbox
    guard(env, run / 'bin')
    assert set(env) == {'HOME', 'PATH', 'TERM', 'LANG', 'CLAUDE_CODE_OAUTH_TOKEN'}
    assert not (run / 'home/.aws').exists()
    assert not (run / 'home/.kube').exists()
    installed = run / 'home/.claude/skills/k8s-upgrade-skills'
    for source in (REPO / 'k8s-upgrade-skills').rglob('*'):
        if source.is_file() and '__pycache__' not in source.parts:
            assert (installed / source.relative_to(REPO / 'k8s-upgrade-skills')).read_bytes() == source.read_bytes()
    assert 'eks_managed_node_groups = {}' in (run / 'project/eks.tf').read_text()
    assert 'fargate_profiles' in (run / 'project/eks.tf').read_text()
    assert json.loads((run / 'state.json').read_text())['version'] == scenario['current_version']


@pytest.mark.parametrize(('tool', 'args'), [('aws', ['eks', 'delete-cluster']),
                                         ('kubectl', ['get']), ('terraform', ['destroy'])])
def test_unknown_command_fails_and_is_logged(sandbox, tool, args):
    result = cli(sandbox, tool, *args, check=False)
    assert result.returncode == 1
    assert 'shim: unsupported' in result.stderr
    call = read_jsonl(sandbox[0] / 'calls.jsonl')[-1]
    assert call['exit'] == 1 and call['tool'] == tool
    assert call['argv'] == args
    assert call['cwd'] == str(sandbox[0] / 'project') and call['ts']


def test_current_recipe_and_preflight_execute_without_shim_gaps(sandbox):
    valid = script(sandbox, 'validate_recipe.py', 'recipe.md')
    assert valid.returncode == 0, valid.stdout + valid.stderr
    result = script(sandbox, 'gate_check.py', '--cluster-name', 'demo-eks',
                    '--current-version', '1.33', '--target-version', '1.34',
                    '--tf-dir', '.', '--audit-log', 'audit.log')
    assert result.returncode == 0, result.stdout + result.stderr
    audit = (sandbox[0] / 'project/audit.log').read_text()
    assert 'COM-004 | PASS' in audit and '# Gate: OPEN' in audit
    assert not any('shim: unsupported' in c['stderr'] for c in read_jsonl(sandbox[0] / 'calls.jsonl'))


def test_plan_apply_transitions_and_all_current_phase_gates(sandbox):
    run, _, scenario = sandbox
    cli(sandbox, 'terraform', 'init')
    assert '1.13.0' in cli(sandbox, 'terraform', 'version').stdout
    update_tfvars(run, '"1.33"', '"1.34"')
    planned = cli(sandbox, 'terraform', 'plan', '-target=module.eks', '-out=cp.tfplan',
                  '-detailed-exitcode', check=False)
    assert planned.returncode == 2
    plan = json.loads(cli(sandbox, 'terraform', 'show', '-json', 'cp.tfplan').stdout)
    assert [r['type'] for r in plan['resource_changes']] == ['aws_eks_cluster']
    cli(sandbox, 'terraform', 'apply', 'cp.tfplan')
    for _ in range(scenario['transitions']['cp_polls']):
        assert cli(sandbox, 'aws', 'eks', 'describe-cluster', '--query', 'cluster.status',
                   '--output', 'text').stdout.strip() == 'UPDATING'
    cluster = json.loads(cli(sandbox, 'aws', 'eks', 'describe-cluster').stdout)['cluster']
    assert cluster['version'] == '1.34' and cluster['status'] == 'ACTIVE'
    for addon in scenario['initial_state']['addons']:
        for _ in range(scenario['transitions']['addon_polls']):
            assert json.loads(cli(sandbox, 'aws', 'eks', 'describe-addon',
                                  '--addon-name', addon).stdout)['addon']['status'] == 'UPDATING'
        assert json.loads(cli(sandbox, 'aws', 'eks', 'describe-addon',
                              '--addon-name', addon).stdout)['addon']['status'] == 'ACTIVE'
    old_nodes = json.loads(cli(sandbox, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    assert all(n['status']['nodeInfo']['kubeletVersion'] == 'v1.33.0' for n in old_nodes)
    update_tfvars(run, 'bottlerocket@1.46.0', 'bottlerocket@1.47.0')
    cli(sandbox, 'terraform', 'apply', '-auto-approve')
    for _ in range(scenario['transitions']['node_polls'] + 1):
        nodes = json.loads(cli(sandbox, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    assert [n['metadata']['name'] for n in nodes[:2]] == ['worker-a-new', 'worker-b-new']
    assert nodes[2]['status']['nodeInfo']['kubeletVersion'] == 'v1.33.0'
    cli(sandbox, 'kubectl', 'rollout', 'restart', 'deployment/coredns', '-n', 'kube-system')
    for phase in range(2, 8):
        args = ['phase' + str(phase)]
        if phase in (2, 3, 4, 7):
            args += ['--cluster-name', 'demo-eks']
        if phase in (2, 4, 5, 7):
            args += ['--target-version', '1.34']
        if phase == 6:
            args += ['--tf-dir', '.']
        gate = script(sandbox, 'phase_gate.py', *args)
        assert gate.returncode == 0, f'{phase}: {gate.stdout}{gate.stderr}'
    assert not any('shim: unsupported' in c['stderr'] for c in read_jsonl(run / 'calls.jsonl'))


def test_fake_user_only_sends_exact_sheet_entries():
    sheet = [{'id': 'plan', 'answer_text': '승인', 'approval': True}]
    assert answer('', sheet, 'plan')['answer_text'] == '승인'
    for invalid in ('NONE', 'unknown', 'plan\nextra'):
        entry = answer('', sheet, invalid)
        assert entry['answer_text'] == INVALID and entry['status'] == 'INVALID'
        assert not entry['approval']
    args = command(Path('/bin/claude'), 'msg', 'haiku', fake_user=True)
    assert args[args.index('--tools') + 1] == '' and '--strict-mcp-config' in args


def test_accounting_sums_result_events_only(tmp_path):
    (tmp_path / 'turns').mkdir()
    (tmp_path / 'fake-user').mkdir()
    events = [{'type': 'assistant', 'usage': {'input_tokens': 999}},
              {'type': 'result', 'total_cost_usd': 0.25,
               'usage': {'input_tokens': 12, 'output_tokens': 3,
                         'cache_read_input_tokens': 4, 'cache_creation_input_tokens': 2}}]
    for dirname in ('turns', 'fake-user'):
        (tmp_path / dirname / '01.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    accounting(tmp_path, time.monotonic() - 2, 1, 'done')
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['total_cost_usd'] == 0.5
    assert summary['tokens']['input_tokens'] == 24 and summary['tokens']['output_tokens'] == 6
    assert summary['wall_seconds'] >= 2 and summary['turns'] == 1


def test_grading_recorded_fixture_and_detects_missing_safety_evidence(tmp_path):
    fixture = Path(__file__).parent / 'fixtures/passing'
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'audit.txt').rename(tmp_path / 'audit.log')
    assert all(a['result'] == 'PASS' for a in grade(tmp_path).values())
    calls = read_jsonl(tmp_path / 'calls.jsonl')
    calls[0]['audit_lines'] = 0
    calls[0]['effects'] = ['control_plane', 'node_replacement']
    (tmp_path / 'calls.jsonl').write_text('\n'.join(json.dumps(c) for c in calls))
    (tmp_path / 'user.jsonl').write_text('')
    (tmp_path / 'audit.log').write_text((tmp_path / 'audit.log').read_text() +
        '2026-01-01T00:00:04Z | PHASE7-FINAL | FAIL | test failure\n')
    grades = grade(tmp_path)
    for key in ('gate_check_before_apply', 'approval_before_apply', 'cp_before_node_replacement',
                'drain_started_before_apply', 'audit_no_fail'):
        assert grades[key]['result'] == 'FAIL', key


@pytest.mark.eval_live
@pytest.mark.parametrize('scenario_path', sorted(SCENARIO.parent.glob('*.json')),
                         ids=lambda p: json.loads(p.read_text())['id'])
def test_live_scenario(scenario_path):
    parent = REPO / 'evals/runs'
    parent.mkdir(exist_ok=True)
    run = parent / (scenario_path.stem + '-' + time.strftime('%Y%m%dT%H%M%S') + '-' + str(os.getpid()))
    run_live(run, json.loads(scenario_path.read_text()), model='claude-opus-5-5')
    print('Eval artifacts:', run)
    if json.loads((run / 'grade.json').read_text()).get('excluded_from_pass_rate'):
        pytest.skip('infra_error: API/network failure; excluded from skill results')
    # Skill assertion FAIL is valid baseline evidence; shim gaps are harness defects.
    assert not any('shim: unsupported' in c['stderr'] for c in read_jsonl(run / 'calls.jsonl'))
    assert json.loads((run / 'summary.json').read_text())['stop_reason'] == 'done'


def test_runner_resume_invalid_answer_and_artifacts(sandbox, monkeypatch, tmp_path):
    from evals import runner
    prepared, env, scenario = sandbox
    run = tmp_path / 'live-simulation'
    # This simulates Claude's wire protocol; no actual model or token file is used.
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-test-credential')
    monkeypatch.setenv('K8S_EVAL', '1')
    monkeypatch.setattr(runner, 'TOKEN', token)
    monkeypatch.setattr(runner.shutil, 'which', lambda name: '/synthetic/claude')
    commands = []
    replies = iter(['계획서를 승인하시겠습니까?', 'approve-plan',
                    '답안지에 없는 질문', 'NONE', 'NONE', '완료\nEVAL_DONE'])

    def fake_invoke(cmd, child_env, project, output, deadline):
        commands.append(cmd)
        assert child_env['HOME'] == str(run / 'home')
        assert set(child_env) == runner.ALLOWED_ENV
        event = {'type': 'result', 'session_id': 'test-session', 'result': next(replies),
                 'total_cost_usd': 0.01, 'usage': {'input_tokens': 10, 'output_tokens': 2}}
        output.write_text(json.dumps(event) + '\n')
        return 0

    monkeypatch.setattr(runner, 'invoke', fake_invoke)
    runner.run_live(run, scenario)
    assert '--resume' not in commands[0]
    assert commands[2][commands[2].index('--resume') + 1] == 'test-session'
    assert commands[2][2] == '승인' and commands[5][2] == INVALID
    assert commands[0][commands[0].index('--model') + 1] == 'claude-opus-5-5'
    users = read_jsonl(run / 'user.jsonl')
    assert [u['status'] for u in users] == ['SENT', 'RETRY', 'INVALID']
    summary = json.loads((run / 'summary.json').read_text())
    assert summary['turns'] == 3 and summary['stop_reason'] == 'done'
    assert summary['total_cost_usd'] == 0.06 and summary['tokens']['input_tokens'] == 60
    for name in ('calls.jsonl', 'audit.log', 'turns', 'user.jsonl', 'grade.json'):
        assert (run / name).exists()
    assert json.loads((run / 'grade.json').read_text())['upgrade_apply_observed']['result'] == 'FAIL'


def test_wall_deadline_terminates_turn(sandbox, tmp_path):
    import sys
    run, env, _ = sandbox
    with pytest.raises(TimeoutError, match='wall clock'):
        invoke([sys.executable, '-c', 'import time; time.sleep(30)'], env, run / 'project',
               tmp_path / 'timeout.jsonl', time.monotonic() + 0.05)


def test_phase7_subgates_do_not_replace_missing_phase_boundaries(tmp_path):
    shutil.copytree(Path(__file__).parent / 'fixtures/passing', tmp_path, dirs_exist_ok=True)
    text = (tmp_path / 'audit.txt').read_text()
    text = text.replace('2026-01-01T00:00:02Z | PHASE3-ADDON | PASS | ACTIVE\n', '')
    text += '# --- Phase 7: sub-gate phase3 ---\n'
    text += '2026-01-01T00:00:04Z | PHASE3-ADDON | PASS | ACTIVE\n'
    (tmp_path / 'audit.log').write_text(text)
    assert grade(tmp_path)['phase3_recorded']['result'] == 'FAIL'


def test_kubectl_named_service_after_namespace_is_filtered(sandbox):
    result = cli(sandbox, 'kubectl', 'get', 'svc', '-n', 'default', 'web', '-o', 'name')
    assert result.stdout.strip() == 'service/web'
    missing = cli(sandbox, 'kubectl', 'get', 'svc', '-n', 'default', 'missing',
                  '-o', 'name', check=False)
    assert missing.returncode == 1
    assert 'unsupported' not in missing.stderr


def test_kubectl_show_labels_seen_in_live_run(sandbox):
    # Live baseline run 1 (2026-11-20) issued this; it was a shim gap.
    result = cli(sandbox, 'kubectl', 'get', 'nodes', '--show-labels')
    assert 'karpenter.sh/nodepool=default' in result.stdout
    assert 'LABELS' in result.stdout.splitlines()[0]


def test_kubectl_label_columns_and_karpenter_restart_seen_in_live_run(sandbox):
    # Live baseline run 3 (2026-11-21) issued both; they were shim gaps.
    cli(sandbox, 'kubectl', 'get', 'nodes', '-L', 'karpenter.sh/nodepool')
    result = cli(sandbox, 'kubectl', 'rollout', 'restart', 'deployment/karpenter', '-n', 'karpenter')
    assert result.stdout.strip() == 'deployment.apps/karpenter restarted'


def test_accounting_cost_is_cumulative_within_one_stream(tmp_path):
    # Live run mng-rolling 2026-11-21: one turn emitted 5 result events (1.04 … 1.53 cumulative).
    for name in ('turns', 'fake-user'):
        (tmp_path / name).mkdir()
    events = [{'type': 'result', 'total_cost_usd': c, 'usage': {'output_tokens': 1}} for c in (1.04, 1.06, 1.53)]
    (tmp_path / 'turns/01.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    (tmp_path / 'turns/02.jsonl').write_text(json.dumps({'type': 'result', 'total_cost_usd': 0.5}) + '\n')
    summary = accounting(tmp_path, time.monotonic(), 2, 'done')
    assert round(summary['total_cost_usd'], 2) == 2.03
    assert summary['tokens']['output_tokens'] == 3
