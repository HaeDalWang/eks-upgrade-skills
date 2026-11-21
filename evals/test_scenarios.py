"""CLI transition replays and handwritten grading evidence for baseline scenarios."""
import json
import shutil
from pathlib import Path

import pytest

from evals.grading import grade, read_jsonl
from evals.runner import SCENARIO, prepare
from evals.test_harness import cli, script, update_tfvars


def sandbox_for(tmp_path, name):
    scenario = json.loads((SCENARIO.parent / (name + '.json')).read_text())
    run = tmp_path / 'run'
    env = prepare(run, scenario, Path('/not-used/claude'))
    return run, env, scenario


def cluster(sandbox):
    return json.loads(cli(sandbox, 'aws', 'eks', 'describe-cluster').stdout)['cluster']


def finish_cp(sandbox):
    run, _, scenario = sandbox
    update_tfvars(run, '"1.33"', '"1.34"')
    cli(sandbox, 'terraform', 'apply', '-target=module.eks')
    for _ in range(scenario['transitions']['cp_polls']):
        assert cluster(sandbox)['status'] == 'UPDATING'
    assert cluster(sandbox)['status'] == 'ACTIVE'


def test_cp_409_cli_accepted_updating_active_then_sync(tmp_path):
    sb = sandbox_for(tmp_path, 'cp-409')
    run, _, scenario = sb
    update_tfvars(run, '"1.33"', '"1.34"')
    cli(sb, 'terraform', 'plan', '-target=module.eks', '-out=cp.plan')
    conflict = cli(sb, 'terraform', 'apply', 'cp.plan', check=False)
    assert conflict.returncode == 1 and 'ResourceInUseException' in conflict.stderr
    assert 'HTTP 409' in conflict.stderr and 'Cannot VersionUpdate' in conflict.stderr
    state = json.loads((run / 'state.json').read_text())
    assert state['status'] == 'UPDATING' and state['cp_target'] == '1.34'
    again = cli(sb, 'terraform', 'apply', 'cp.plan', check=False)
    assert again.returncode == 1 and again.stderr == conflict.stderr
    assert read_jsonl(run / 'calls.jsonl')[-1]['cp_status'] == 'UPDATING'
    for _ in range(scenario['transitions']['cp_polls'] - 1):
        assert cluster(sb)['status'] == 'UPDATING'
    active = cluster(sb)
    assert active['status'] == 'ACTIVE' and active['version'] == '1.34'
    cli(sb, 'terraform', 'apply', 'cp.plan')
    assert read_jsonl(run / 'calls.jsonl')[-1]['effects'] == ['cp_sync']
    assert script(sb, 'phase_gate.py', 'phase2', '--cluster-name', 'demo-eks',
                  '--target-version', '1.34').returncode == 0


def test_addon_sequential_cli_each_addon_needs_own_polls(tmp_path):
    sb = sandbox_for(tmp_path, 'addon-sequential')
    finish_cp(sb)
    run, _, scenario = sb
    for _ in range(scenario['transitions']['addon_polls']):
        cli(sb, 'aws', 'eks', 'describe-addon', '--addon-name', 'kube-proxy')
    for name in ('vpc-cni', 'coredns', 'kube-proxy'):
        state = json.loads((run / 'state.json').read_text())
        assert state['addon_queue'][0] == name
        for _ in range(scenario['transitions']['addon_polls'] - 1):
            result = cli(sb, 'aws', 'eks', 'describe-addon', '--addon-name', name)
            assert json.loads(result.stdout)['addon']['status'] == 'UPDATING'
        result = cli(sb, 'aws', 'eks', 'describe-addon', '--addon-name', name)
        assert json.loads(result.stdout)['addon']['status'] == 'ACTIVE'
    assert json.loads((run / 'state.json').read_text())['addon_queue'] == []
    assert script(sb, 'phase_gate.py', 'phase3', '--cluster-name', 'demo-eks').returncode == 0


def test_addon_gate_exit_instrumentation_records_warn_then_pass(tmp_path):
    sb = sandbox_for(tmp_path, 'addon-sequential')
    finish_cp(sb)
    assert script(sb, 'phase_gate.py', 'phase3', '--cluster-name', 'demo-eks').returncode == 2
    for _ in range(10):
        result = script(sb, 'phase_gate.py', 'phase3', '--cluster-name', 'demo-eks')
        if result.returncode == 0:
            break
    assert result.returncode == 0
    gates = [c for c in read_jsonl(sb[0] / 'calls.jsonl') if c['tool'] == 'python3']
    assert gates[0]['exit'] == 2 and gates[-1]['exit'] == 0
    assert all(c['audit_end'] > c['audit_lines'] and c['finished'] >= c['ts'] for c in gates)


def test_mng_rolling_cli_targeted_ami_plan_nodes_and_phase5_skip(tmp_path):
    sb = sandbox_for(tmp_path, 'mng-rolling')
    run, _, scenario = sb
    project = (run / 'project/eks.tf').read_text()
    assert 'system-node = {' in project and 'module "karpenter"' not in project
    assert 'release_version = var.eks_node_ami_alias_al2023' in project
    assert 'fargate_profiles' not in project
    for crd in ('nodeclaims.karpenter.sh', 'nodepools.karpenter.sh'):
        assert cli(sb, 'kubectl', 'get', 'crd', crd, check=False).returncode == 1
    assert json.loads(cli(sb, 'aws', 'eks', 'list-nodegroups').stdout)['nodegroups'] == ['demo-system']
    assert json.loads(cli(sb, 'aws', 'eks', 'list-fargate-profiles').stdout)['fargateProfileNames'] == []
    finish_cp(sb)
    old = json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    update_tfvars(run, scenario['initial_state']['ami_alias'], '1.34.0-20261121')
    target = '-target=module.eks.module.eks_managed_node_group'
    cli(sb, 'terraform', 'plan', target, '-out=mng.plan')
    plan = json.loads(cli(sb, 'terraform', 'show', '-json', 'mng.plan').stdout)
    assert [c['type'] for c in plan['resource_changes']] == ['aws_eks_node_group']
    cli(sb, 'terraform', 'apply', 'mng.plan')
    for _ in range(scenario['transitions']['node_polls']):
        ng = json.loads(cli(sb, 'aws', 'eks', 'describe-nodegroup', '--nodegroup-name', 'demo-system').stdout)
        assert ng['nodegroup']['status'] == 'UPDATING'
        assert json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items'] == old
    ng = json.loads(cli(sb, 'aws', 'eks', 'describe-nodegroup', '--nodegroup-name', 'demo-system').stdout)
    assert ng['nodegroup']['status'] == 'ACTIVE' and ng['nodegroup']['version'] == '1.34'
    new = json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    assert {n['metadata']['name'] for n in old}.isdisjoint(n['metadata']['name'] for n in new)
    assert all(n['status']['nodeInfo']['kubeletVersion'] == 'v1.34.0' for n in new)
    assert script(sb, 'phase_gate.py', 'phase4', '--cluster-name', 'demo-eks', '--target-version', '1.34').returncode == 0
    assert script(sb, 'phase_gate.py', 'phase5', '--target-version', '1.34').returncode == 0
    assert 'PHASE5-KARPENTER | PASS | Karpenter 미사용 — SKIP' in (run / 'project/audit.log').read_text()


def handwritten_fixture(run, name):
    """Synthetic evidence, never presented as live agent performance."""
    scenario = json.loads((SCENARIO.parent / (name + '.json')).read_text())
    (run / 'scenario.json').write_text(json.dumps(scenario))
    phase5 = 'Karpenter 미사용 — SKIP' if name == 'mng-rolling' else 'Ready'
    rules = [('COM-002', 'PASS', 'minor +1'), ('INF-004', 'PASS', 'safe plan'),
             ('DRAIN-P2', 'INFO', 'DrainWatch started'), ('PHASE2-CP', 'PASS', 'ACTIVE'),
             ('PHASE3-ADDON', 'WARN', 'UPDATING'), ('PHASE3-ADDON', 'PASS', 'all ACTIVE'),
             ('DRAIN-P4' if name == 'mng-rolling' else 'DRAIN-P5', 'INFO', 'DrainWatch started'),
             ('PHASE4-DATAPLANE', 'PASS', 'Ready'), ('PHASE5-KARPENTER', 'PASS', phase5),
             ('PHASE6-TFSYNC', 'PASS', 'no diff'), ('PHASE7-FINAL', 'PASS', 'final')]
    (run / 'audit.log').write_text(''.join(f'2026-01-01T00:00:00Z | {r} | {s} | {d}\n' for r, s, d in rules))
    (run / 'user.jsonl').write_text(json.dumps({'ts': '2026-01-01T00:00:00Z', 'approval': True,
                                             'status': 'SENT', 'answer_text': '승인'}) + '\n')
    calls = [call('terraform', ['apply', '-target=module.eks'], 3, ['control_plane'])]
    if name == 'cp-409':
        calls[0].update(exit=1, stderr='ResourceInUseException: HTTP 409')
        calls.append(call('terraform', ['apply', '-target=module.eks'], 3, ['cp_sync']))
    if name == 'addon-sequential':
        calls += [call('python3', ['phase_gate.py', 'phase3'], 4, [], exit=2,
                       audit_end=5, ts='2026-01-01T00:00:02Z', finished='2026-01-01T00:00:03Z'),
                  call('python3', ['phase_gate.py', 'phase3'], 5, [], audit_end=6,
                       ts='2026-01-01T00:00:33Z', finished='2026-01-01T00:00:34Z')]
    effects = ['node_replacement'] + (['mng_rollout'] if name == 'mng-rolling' else [])
    calls.append(call('terraform', ['apply'], 7, effects, ts='2026-01-01T00:00:35Z'))
    write_calls(run, calls)
    return calls


def call(tool, argv, audit_lines, effects, **overrides):
    return {'tool': tool, 'argv': argv, 'audit_lines': audit_lines, 'effects': effects,
            'ts': '2026-01-01T00:00:01Z', 'exit': 0, 'stderr': '',
            'cp_status': 'ACTIVE', 'cp_version': '1.34', **overrides}


def write_calls(run, calls):
    (run / 'calls.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in calls))


@pytest.mark.parametrize('name', ['cp-409', 'addon-sequential', 'mng-rolling'])
def test_scenario_grading_handwritten_passing_fixture(tmp_path, name):
    handwritten_fixture(tmp_path, name)
    grades = grade(tmp_path)
    assert all(g['result'] == 'PASS' for g in grades.values()), grades
    assert 'fargate_restarted' not in grades


@pytest.mark.parametrize(('name', 'assertion'), [
    ('cp-409', 'no_apply_while_updating'),
    ('addon-sequential', 'addons_before_nodes'),
    ('mng-rolling', 'mng_drain_before_apply')])
def test_scenario_grading_handwritten_failing_fixture(tmp_path, name, assertion):
    calls = handwritten_fixture(tmp_path, name)
    if name == 'cp-409':
        calls[1]['cp_status'] = 'UPDATING'
    else:
        calls[-1]['audit_lines'] = 4 if name == 'addon-sequential' else 6
    write_calls(tmp_path, calls)
    assert grade(tmp_path)[assertion]['result'] == 'FAIL'


@pytest.mark.parametrize('fault', ['no-sync', 'duplicate-sync', 'sync-after-phase2', 'no-phase2'])
def test_cp_409_grading_requires_one_sync_before_phase2(tmp_path, fault):
    calls = handwritten_fixture(tmp_path, 'cp-409')
    if fault == 'no-sync': calls.pop(1)
    if fault == 'duplicate-sync': calls.insert(2, dict(calls[1]))
    if fault == 'sync-after-phase2': calls[1]['audit_lines'] = 4
    if fault == 'no-phase2':
        p = tmp_path / 'audit.log'
        p.write_text(p.read_text().replace('PHASE2-CP | PASS', 'PHASE2-CP | WARN'))
    write_calls(tmp_path, calls)
    assert grade(tmp_path)['one_post_active_sync']['result'] == 'FAIL'


@pytest.mark.parametrize('fault', ['no-retry', 'too-soon', 'node-before-retry', 'early-retry-then-wait'])
def test_addon_grading_requires_wait_and_gate_retry(tmp_path, fault):
    calls = handwritten_fixture(tmp_path, 'addon-sequential')
    if fault == 'no-retry': calls.pop(2)
    if fault == 'too-soon': calls[2]['ts'] = '2026-01-01T00:00:04Z'
    if fault == 'node-before-retry': calls.insert(2, calls.pop())
    if fault == 'early-retry-then-wait':
        calls.insert(2, {**calls[1], 'ts': '2026-01-01T00:00:04Z',
                         'finished': '2026-01-01T00:00:05Z'})
        calls[3]['ts'] = '2026-01-01T00:00:35Z'
    write_calls(tmp_path, calls)
    assert grade(tmp_path)['addon_gate_retried_after_wait']['result'] == 'FAIL'


def test_normal_grading_fixture_is_identical_to_baseline(tmp_path):
    fixture = Path(__file__).parent / 'fixtures/passing'
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'audit.txt').rename(tmp_path / 'audit.log')
    assert grade(tmp_path) == json.loads((fixture / 'expected-grade.json').read_text())


@pytest.mark.parametrize('name', ['cp-409', 'addon-sequential', 'mng-rolling'])
def test_batch1_grading_identical_to_8b75581_snapshot(tmp_path, name):
    handwritten_fixture(tmp_path, name)
    expected = json.loads((Path(__file__).parent / 'fixtures/batch1-expected-grade.json').read_text())
    assert grade(tmp_path) == expected[name]
