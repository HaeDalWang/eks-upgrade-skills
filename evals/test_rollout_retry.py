"""Offline rollout signals and real gate retries against fake CLIs."""
import json
from datetime import datetime, timezone

import pytest

from evals.test_harness import cli, script, update_tfvars
from evals.test_scenarios import finish_cp, sandbox_for


def seed_apply(run):
    timestamp = datetime.now(timezone.utc).isoformat()
    (run / 'project/audit.log').write_text(
        f'{timestamp} | APPLY-GUARD | - | PASS | offline apply\n')


def test_nodeclaim_drifted_during_rollout_false_after(sandbox):
    run, _, scenario = sandbox
    finish_cp(sandbox)
    update_tfvars(run, scenario['initial_state']['ami_alias'], scenario['target_ami'])
    cli(sandbox, 'terraform', 'apply', '-target=kubectl_manifest.karpenter_default_node_class')
    for expected in ['True'] * scenario['transitions']['node_polls'] + ['False']:
        claims = json.loads(cli(sandbox, 'kubectl', 'get', 'nodeclaims', '-o', 'json').stdout)
        assert claims['items']
        assert all(next(c['status'] for c in claim['status']['conditions']
                        if c['type'] == 'Drifted') == expected for claim in claims['items'])


@pytest.mark.parametrize('name,phase,target', [
    ('normal-karpenter-fargate', 'phase5', 'kubectl_manifest.karpenter_default_node_class'),
    ('mng-rolling', 'phase4', 'module.eks.module.eks_managed_node_group'),
])
def test_phase_gate_sees_shim_rollout_and_retries(tmp_path, name, phase, target):
    sb = sandbox_for(tmp_path, name)
    run, _, scenario = sb
    finish_cp(sb)
    update_tfvars(run, scenario['initial_state']['ami_alias'], scenario['target_ami'])
    cli(sb, 'terraform', 'apply', '-target=' + target)
    seed_apply(run)
    args = [phase, '--target-version', '1.34', '--audit-log', 'audit.log']
    if phase == 'phase4':
        args += ['--cluster-name', 'demo-eks']
    # Rerunning only the gate (as NEXT: RETRY says) must end in PASS, never in a false FAIL.
    codes = []
    for _ in range(scenario['transitions']['node_polls'] + 1):
        result = script(sb, 'phase_gate.py', *args)
        codes.append(result.returncode)
        if result.returncode == 0:
            break
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stdout.splitlines()[-1] == 'NEXT: RETRY wait 30s for the node rollout, then rerun gate'
    assert codes[0] == 2 and codes[-1] == 0, codes


@pytest.mark.parametrize('name,phase,target,watch', [
    ('normal-karpenter-fargate', 'phase5', 'kubectl_manifest.karpenter_default_node_class',
     ['kubectl', 'get', 'nodeclaims', '-o', 'yaml']),
    ('mng-rolling', 'phase4', 'module.eks.module.eks_managed_node_group',
     ['aws', 'eks', 'describe-nodegroup', '--cluster-name', 'demo-eks', '--nodegroup-name', 'demo-system']),
])
@pytest.mark.parametrize('watches', [0, 1, 2])
def test_gate_never_false_fails_after_any_watch_count(tmp_path, name, phase, target, watch, watches):
    # Review finding: the result depended on how many watch polls ran before the gate.
    sb = sandbox_for(tmp_path, name)
    run, _, scenario = sb
    finish_cp(sb)
    update_tfvars(run, scenario['initial_state']['ami_alias'], scenario['target_ami'])
    cli(sb, 'terraform', 'apply', '-target=' + target)
    seed_apply(run)
    for _ in range(watches):
        cli(sb, *watch)
    args = [phase, '--target-version', '1.34', '--audit-log', 'audit.log']
    if phase == 'phase4':
        args += ['--cluster-name', 'demo-eks']
    for _ in range(scenario['transitions']['node_polls'] + 1):
        rc = script(sb, 'phase_gate.py', *args).returncode
        assert rc in (0, 2)
        if rc == 0:
            break
    assert rc == 0


def test_namespaced_sorted_events_succeeds(sandbox):
    result = cli(sandbox, 'kubectl', 'get', 'events', '-n', 'default', '--sort-by=.lastTimestamp')
    assert result.stdout.strip() == 'No resources found.'


def test_terraform_show_text_saved_plan_succeeds(sandbox):
    run, _, _ = sandbox
    update_tfvars(run, '"1.33"', '"1.34"')
    planned = cli(sandbox, 'terraform', 'plan', '-target=module.eks', '-out=cp.tfplan')
    shown = cli(sandbox, 'terraform', 'show', '-no-color', 'cp.tfplan')
    assert shown.stdout == planned.stdout
    assert 'aws_eks_cluster' in shown.stdout
    assert '1.33' in shown.stdout and '1.34' in shown.stdout
    assert 'Plan: 0 to add, 1 to change, 0 to destroy.' in shown.stdout
