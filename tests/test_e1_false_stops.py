"""Offline regressions for E1 false stops; all external commands are mocked."""
import copy
import json
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import gate_check
import lib
import phase_gate


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    lib.reset_gate()
    monkeypatch.setattr(lib, 'skill_revision', lambda: 'test-revision')


def node(name='worker', tainted=False, version='v1.34.0'):
    return {'metadata': {'name': name}, 'spec': {'taints': [
        {'key': 'ToBeDeletedByClusterAutoscaler'}] if tainted else []},
        'status': {'nodeInfo': {'kubeletVersion': version},
                   'conditions': [{'type': 'Ready', 'status': 'True'}]}}


def pod(name='api-old', owner='api-hash', kind='ReplicaSet'):
    return {'metadata': {'name': name, 'namespace': 'app',
            'creationTimestamp': '2020-01-01T00:00:00Z',
            'labels': {'pod-template-hash': 'hash'},
            'ownerReferences': [{'kind': kind, 'name': owner, 'controller': True}]},
            'spec': {'nodeName': 'worker'}, 'status': {'phase': 'Running',
            'containerStatuses': [{'name': 'app', 'ready': False}]}}


def seed(path, rule, scope, field, values, result='INFO'):
    value = json.dumps(values) if field == 'addresses' else ','.join(values)
    detail = ' '.join(shlex.quote(k + '=' + v) for k, v in dict(scope, **{field: value}).items())
    lib.audit_append(str(path), rule, result, detail)


def mock_phase4(monkeypatch, nodes=None, pods=None):
    def run(args, **kwargs):
        resource = args[2]
        data = {'nodes': nodes if nodes is not None else [node()],
                'pods': pods or [], 'events': []}[resource]
        return subprocess.CompletedProcess(args, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    monkeypatch.setattr(phase_gate, 'mng_rollout_in_progress', lambda _: False)


@pytest.mark.parametrize('state', ['gone', 'terminating', 'existing'])
def test_s8_missing_or_terminating_daemonset_is_transient(state, tmp_path, monkeypatch):
    p = pod(kind='DaemonSet', owner='agent')
    if state == 'gone':
        p['spec']['nodeName'] = 'removed'
    if state == 'terminating':
        p['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    result = phase_gate.classify_pods({'items': [p]}, {'items': [node()]})
    assert bool(result.blocking) == (state == 'existing')
    assert bool(result.transient) == (state != 'existing')
    mock_phase4(monkeypatch, pods=[p])
    log = tmp_path / 'audit'
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == (1 if state == 'existing' else 2)
    if state != 'existing':
        assert ('node gone' if state == 'gone' else 'terminating') in log.read_text()


@pytest.mark.parametrize('apply_rule', ['APPLY-GUARD', 'USER-APPLY'])
@pytest.mark.parametrize('age,mixed,expected', [(30, False, 2), (1801, False, 1),
                                               (30, True, 1), (None, False, 1)])
def test_s9_ca_rollout_is_bounded(apply_rule, age, mixed, expected, tmp_path, monkeypatch, capsys):
    log = tmp_path / 'audit'
    if age is not None:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        log.write_text(f'{ts} | {apply_rule} | PASS | apply\n')
    nodes = [node(tainted=True, version='v1.33.0')]
    if mixed:
        nodes.append(node('other', version='v1.33.0'))
    mock_phase4(monkeypatch, nodes=nodes)
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == expected
    if expected == 2:
        assert 'ROLLOUT ' in log.read_text()
        assert capsys.readouterr().out.splitlines()[-1].startswith('NEXT: RETRY')


@pytest.mark.parametrize('kind,owner,expected', [('ReplicaSet', 'api-hash', 'Deployment/api'),
    ('Job', 'batch', 'Job/batch'), ('StatefulSet', 'db', 'StatefulSet/db'),
    ('DaemonSet', 'agent', 'DaemonSet/agent'), ('', '', 'Pod/api-old')])
def test_s10_workload_identity(kind, owner, expected):
    p = pod(kind=kind, owner=owner)
    if not kind:
        p['metadata']['ownerReferences'] = []
    assert phase_gate.workload_identity(p) == 'app/' + expected


@pytest.mark.parametrize('scenario,expected', [('same', 2), ('new', 1), ('other-target', 1),
    ('other-cluster', 1), ('absent', 1), ('latest-empty', 1), ('invalid', 1)])
def test_s10_only_latest_matching_baseline_softens_failure(scenario, expected, tmp_path, monkeypatch):
    log = tmp_path / 'audit'
    scope = {'cluster_name': 'demo', 'target_version': '1.34'}
    seed(log, 'BASELINE-PODS', {'cluster_name': 'older', 'target_version': '1.33'},
         'workloads', ['app/Deployment/api'])
    if scenario != 'absent':
        actual = dict(scope)
        if scenario == 'other-target':
            actual['target_version'] = '1.35'
        if scenario == 'other-cluster':
            actual['cluster_name'] = 'elsewhere'
        seed(log, 'BASELINE-PODS', actual, 'workloads', ['app/Deployment/api'])
        if scenario == 'latest-empty':
            seed(log, 'BASELINE-PODS', scope, 'workloads', [])
        if scenario == 'invalid':
            lib.audit_append(str(log), 'BASELINE-PODS', 'INFO',
                             'cluster_name=demo target_version=1.34 workloads="')
    p = pod(owner='new-hash' if scenario == 'new' else 'api-hash')
    mock_phase4(monkeypatch, pods=[p])
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == expected
    if expected == 2:
        assert 'baseline 기존 장애' in log.read_text()


def test_s10_phase0_records_classified_app_workloads(monkeypatch):
    p = pod()
    system = copy.deepcopy(p)
    system['metadata']['namespace'] = 'kube-system'
    monkeypatch.setattr(gate_check, 'kubectl_json', lambda resource, **kw:
                        {'items': [node()] if resource == 'nodes' else [p, system]})
    gate_check.record_pod_baseline('demo', '1.34')
    row = lib.parse_audit_line(lib._gate.audit_lines[-1])
    assert row['rule_id'] == 'BASELINE-PODS'
    assert 'workloads=app/Deployment/api' in row['detail']
    assert 'kube-system' not in row['detail']


@pytest.mark.parametrize('scenario,expected', [('same', 2), ('extra', 1), ('other-dir', 1),
                                            ('absent', 1), ('latest-empty', 1), ('invalid', 1)])
def test_s13_recreate_addresses_match_latest_directory_baseline(scenario, expected, tmp_path, monkeypatch):
    log = tmp_path / 'audit'
    address = 'module.pool.aws_eks_node_group.workers["a,b"]'
    if scenario != 'absent':
        seed(log, 'BASELINE-RECREATE', {'tf_dir': str(tmp_path if scenario != 'other-dir' else tmp_path / 'other')},
             'addresses', [address])
    if scenario == 'latest-empty':
        seed(log, 'BASELINE-RECREATE', {'tf_dir': str(tmp_path)}, 'addresses', [])
    if scenario == 'invalid':
        lib.audit_append(str(log), 'BASELINE-RECREATE', 'INFO',
                         f'tf_dir={tmp_path} addresses=invalid')
    addresses = [address, 'aws_eks_node_group.new'] if scenario == 'extra' else [address]
    changes = [{'address': a, 'type': 'aws_eks_node_group',
                'change': {'actions': ['delete', 'create']}} for a in addresses]
    def run(args, **kw):
        return subprocess.CompletedProcess(args, 0, json.dumps({'resource_changes': changes}), '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert phase_gate.gate_phase6(str(tmp_path / '.'), str(log)) == expected
    if expected == 2:
        assert 'Phase 0부터 있던 recreate drift' in log.read_text()


def test_s13_inf004_records_full_indexed_addresses(tmp_path):
    address = 'module.pool.aws_eks_node_group.workers["a,b"]'
    gate_check.check_inf004(f'# {address} must be replaced\n-/+ resource "aws_eks_node_group" "workers" {{', str(tmp_path))
    rows = [lib.parse_audit_line(line) for line in lib._gate.audit_lines]
    row = next(r for r in rows if r['rule_id'] == 'BASELINE-RECREATE')
    fields = dict(token.split('=', 1) for token in shlex.split(row['detail']))
    assert json.loads(fields['addresses']) == [address]
    assert fields['tf_dir'] == str(tmp_path.resolve())


@pytest.mark.parametrize('rule', ['BASELINE-PODS', 'BASELINE-RECREATE'])
def test_baseline_records_cannot_be_forged(rule, tmp_path):
    script = Path(phase_gate.__file__).with_name('audit_event.py')
    log = tmp_path / 'audit'
    result = subprocess.run([sys.executable, str(script), '--audit-log', str(log),
        '--rule-id', rule, '--result', 'INFO', '--detail', 'forged'], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'reserved' in result.stderr
    assert not log.exists()


def test_s10_phase7_inherits_baseline_warning(tmp_path, monkeypatch, capsys):
    log = tmp_path / 'audit'
    seed(log, 'BASELINE-PODS', {'cluster_name': 'demo', 'target_version': '1.34'},
         'workloads', ['app/Deployment/api'])
    mock_phase4(monkeypatch, pods=[pod(name='api-replacement')])
    run_phase4 = phase_gate.run_cmd
    def run(args, **kwargs):
        if args[:3] == ['aws', 'eks', 'list-insights']:
            return subprocess.CompletedProcess(args, 0, '{"insights": []}', '')
        return run_phase4(args, **kwargs)
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    monkeypatch.setattr(phase_gate, 'gate_phase2', lambda *a, **kw: 0)
    monkeypatch.setattr(phase_gate, 'gate_phase3', lambda *a, **kw: 0)
    assert phase_gate.gate_phase7('demo', '1.34', str(log)) == 2
    assert log.read_text().count('BASELINE-PODS') == 1
    assert 'baseline 기존 장애' in log.read_text()
    assert capsys.readouterr().out.splitlines()[-1].startswith('NEXT: ASK_USER')


def test_s10_new_blocking_workload_still_fails_with_existing_warning(tmp_path, monkeypatch):
    log = tmp_path / 'audit'
    seed(log, 'BASELINE-PODS', {'cluster_name': 'demo', 'target_version': '1.34'},
         'workloads', ['app/Deployment/api'])
    mock_phase4(monkeypatch, pods=[pod(), pod(name='new-pod', owner='new-hash')])
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == 1
    assert 'baseline 기존 장애' in log.read_text()
    assert 'BLOCKING Pod 1개: app/new-pod' in log.read_text()


def test_s10_failed_refresh_invalidates_previous_baseline(tmp_path, monkeypatch):
    log = tmp_path / 'audit'
    scope = {'cluster_name': 'demo', 'target_version': '1.34'}
    seed(log, 'BASELINE-PODS', scope, 'workloads', ['app/Deployment/api'])
    monkeypatch.setattr(gate_check, 'kubectl_json', lambda *a, **kw: None)
    gate_check.record_pod_baseline('demo', '1.34')
    lib.audit_flush(str(log))
    assert lib.baseline_values(str(log), 'BASELINE-PODS', scope, 'workloads') is None
    mock_phase4(monkeypatch, pods=[pod()])
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == 1


@pytest.mark.parametrize('plan', ['-/+ resource "aws_eks_node_group" "workers" {',
                                 '# aws_eks_node_group.workers must be replaced\n-/+ resource "aws_eks_node_group" "workers" {'])
def test_s13_incomplete_address_is_unknown_instead_of_guessed(plan, tmp_path):
    gate_check.check_inf004(plan, str(tmp_path))
    log = tmp_path / 'audit'
    lib.audit_flush(str(log))
    values = lib.baseline_values(str(log), 'BASELINE-RECREATE',
                                {'tf_dir': str(tmp_path)}, 'addresses')
    if plan.startswith('-/+'):
        assert values is None
        assert 'reason=' in log.read_text()
    else:
        assert values == {'aws_eks_node_group.workers'}


def test_s13_later_other_directory_does_not_shadow_matching_baseline(tmp_path):
    log = tmp_path / 'audit'
    scope = {'tf_dir': str(tmp_path)}
    seed(log, 'BASELINE-RECREATE', scope, 'addresses', ['aws_eks_node_group.old'])
    seed(log, 'BASELINE-RECREATE', scope, 'addresses', ['aws_eks_node_group.current'])
    seed(log, 'BASELINE-RECREATE', {'tf_dir': str(tmp_path / 'other')}, 'addresses',
         ['aws_eks_node_group.other'])
    assert lib.baseline_values(str(log), 'BASELINE-RECREATE', scope, 'addresses') == {
        'aws_eks_node_group.current'}


@pytest.mark.parametrize('phase,ready,node_name', [('Running', True, 'worker'), ('Succeeded', False, 'removed')])
def test_s8_healthy_pods_stay_healthy(phase, ready, node_name):
    p = pod()
    p['status'] = {'phase': phase, 'containerStatuses': [{'name': 'app', 'ready': ready}]}
    p['spec']['nodeName'] = node_name
    p['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    result = phase_gate.classify_pods({'items': [p]}, {'items': [node()]})
    assert not (result.transient or result.blocking or result.stale)


def test_s10_transient_pod_is_not_baselined(monkeypatch):
    p = pod()
    p['spec']['nodeName'] = 'removed'  # TRANSIENT at Phase 0, not broken
    monkeypatch.setattr(gate_check, 'kubectl_json', lambda resource, **kw:
                        {'items': [node()] if resource == 'nodes' else [p]})
    gate_check.record_pod_baseline('demo', '1.34')
    assert "workloads=''" in lib._gate.audit_lines[-1]


def test_s13_tainted_address_is_recorded(tmp_path):
    gate_check.check_inf004('# aws_instance.x is tainted, so must be replaced\n'
                            '-/+ resource "aws_instance" "x" {', str(tmp_path))
    row = next(lib.parse_audit_line(l) for l in lib._gate.audit_lines if 'BASELINE-RECREATE' in l)
    fields = dict(token.split('=', 1) for token in shlex.split(row['detail']))
    assert json.loads(fields['addresses']) == ['aws_instance.x']


@pytest.fixture(autouse=True)
def empty_statefulsets_for_existing_scenarios(monkeypatch):
    # Existing scenarios have no StatefulSets; the new rollout query must stay offline.
    monkeypatch.setattr(phase_gate, "kubectl_json", lambda *args, **kwargs: {"items": []})
