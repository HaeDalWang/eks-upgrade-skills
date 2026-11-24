"""Offline failure gates, report generation and stop-grading regressions."""
import json
import shutil

import pytest

from evals.grading import grade, read_jsonl
from evals.test_harness import cli, script, update_tfvars
from evals.test_rollout_retry import seed_apply
from evals.test_scenarios import finish_cp, sandbox_for


def approve_plan(sb):
    for name, args in [
        ('discover.py', ['--tf-dir', '.', '--target-version', '1.34']),
        ('gate_check.py', ['--cluster-name', 'demo-eks', '--current-version', '1.33', '--target-version', '1.34']),
        ('upgrade_plan.py', ['create', '--cluster-name', 'demo-eks', '--current-version', '1.33',
                             '--target-version', '1.34', '--tf-dir', '.', '--output-language', 'ko',
                             '--node-pool', 'MNG']),
        ('upgrade_plan.py', ['approve', '--answer', '승인']),
    ]:
        result = script(sb, name, *args)
        assert result.returncode in ((0, 2) if name in ('discover.py', 'gate_check.py') else (0,)), result.stdout + result.stderr


@pytest.mark.parametrize('name,phase,prefix,next_text', [
    ('pv-az-gap', 'phase4', 'PV-AZ ', 'node group desired +1'),
    ('addon-conflict', 'phase3', 'CONFLICT ', 'resolve_conflicts_on_update = "PRESERVE"'),
])
def test_shim_failure_gate_stops_with_specific_next_and_failed_report(tmp_path, name, phase, prefix, next_text):
    sb = sandbox_for(tmp_path, name)
    run, _, scenario = sb
    approve_plan(sb)
    initial = json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    if name == 'pv-az-gap':
        assert {n['metadata']['labels']['topology.kubernetes.io/zone'] for n in initial} == {
            'ap-northeast-2a', 'ap-northeast-2c'}
    finish_cp(sb)
    if phase == 'phase4':
        update_tfvars(run, scenario['initial_state']['ami_alias'], scenario['target_ami'])
        cli(sb, 'terraform', 'apply', '-target=module.eks.module.eks_managed_node_group')
        for _ in range(scenario['transitions']['node_polls'] + 1):
            cli(sb, 'aws', 'eks', 'describe-nodegroup', '--nodegroup-name', 'demo-system')
        replacement_nodes = json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
        assert {n['metadata']['labels']['topology.kubernetes.io/zone'] for n in replacement_nodes} == {
            'ap-northeast-2a'}
    else:
        for addon in ('vpc-cni', 'coredns', 'kube-proxy'):
            actual = json.loads(cli(sb, 'aws', 'eks', 'describe-addon', '--addon-name', addon).stdout)['addon']
            assert actual['status'] == ('UPDATE_FAILED' if addon == 'vpc-cni' else 'ACTIVE')
    args = [phase, '--cluster-name', 'demo-eks']
    if phase == 'phase4':
        args += ['--target-version', '1.34']
    result = script(sb, 'phase_gate.py', *args)
    assert result.returncode == 1, result.stdout + result.stderr
    assert prefix in result.stdout
    assert result.stdout.splitlines()[-1].startswith('NEXT: STOP ')
    assert next_text in result.stdout.splitlines()[-1]
    reported = script(sb, 'report.py')
    assert reported.returncode == 0, reported.stdout + reported.stderr
    assert list((run / 'project').glob('*FAILED*.md'))
    shutil.copyfile(run / 'project/audit.log', run / 'audit.log')
    scenario_path = run / 'scenario.json'
    configured = json.loads(scenario_path.read_text())
    configured['safety_checks_version'] = 0  # This deterministic gate replay does not mimic an agent's whole workflow.
    scenario_path.write_text(json.dumps(configured))
    grades = grade(run)
    assert all(g['result'] == 'PASS' for g in grades.values()), grades
    # Prove stop assertion rejects any subsequent apply, even a failed attempt.
    calls = read_jsonl(run / 'calls.jsonl')
    calls.append(dict(calls[-1], tool='terraform', argv=['apply'], exit=1,
                      audit_lines=len((run / 'audit.log').read_text().splitlines())))
    (run / 'calls.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in calls))
    assertion = 'pv_az_stop' if phase == 'phase4' else 'conflict_stop'
    assert grade(run)[assertion]['result'] == 'FAIL'


def test_pv_az_gap_during_mng_rollout_uses_retry(tmp_path):
    sb = sandbox_for(tmp_path, 'pv-az-gap')
    run, _, scenario = sb
    finish_cp(sb)
    update_tfvars(run, scenario['initial_state']['ami_alias'], scenario['target_ami'])
    cli(sb, 'terraform', 'apply', '-target=module.eks.module.eks_managed_node_group')
    seed_apply(run)
    result = script(sb, 'phase_gate.py', 'phase4', '--cluster-name', 'demo-eks', '--target-version', '1.34')
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == 'NEXT: RETRY wait 30s for the node rollout, then rerun gate'
