"""Cause-specific STOP routing, including cumulative logs and Phase 7."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import phase_gate as gate


def reply(args, data=None, code=0):
    return subprocess.CompletedProcess(args, code, json.dumps(data or {}), '')


@pytest.mark.parametrize('conflict', [True, False])
def test_addon_conflict_stop_and_other_bad_state_unchanged(tmp_path, monkeypatch, capsys, conflict):
    def run(args, **kwargs):
        if 'list-addons' in args:
            return reply(args, {'addons': ['vpc-cni']})
        if 'describe-addon' in args:
            return reply(args, {'addon': {'status': 'UPDATE_FAILED', 'health': {'issues': [
                {'code': 'ConfigurationConflict' if conflict else 'Other', 'message': 'field conflict'}]}}})
        return reply(args, {'items': [{'metadata': {'name': 'dns'}, 'status': {
            'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}]})
    monkeypatch.setattr(gate, 'run_cmd', run)
    audit = tmp_path / 'audit.log'
    assert gate.gate_phase3('demo', str(audit)) == 1
    output = capsys.readouterr().out.splitlines()[-1]
    if conflict:
        assert 'CONFLICT vpc-cni: field conflict' in audit.read_text()
        assert output == ('NEXT: STOP add-on ConfigurationConflict (vpc-cni); set '
                          'resolve_conflicts_on_update = "PRESERVE" on that aws_eks_addon, '
                          'then rerun the add-on step')
    else:
        assert '비정상 Add-on: vpc-cni(UPDATE_FAILED)' in audit.read_text()
        assert 'CONFLICT ' not in audit.read_text()
        assert output == 'NEXT: STOP gate failed; resolve FAIL records before retrying'


@pytest.fixture
def zonal(tmp_path, monkeypatch):
    audit = tmp_path / 'audit.log'
    now = datetime.now(timezone.utc)
    node = {'metadata': {'name': 'worker', 'labels': {'topology.kubernetes.io/zone': 'zone-a'}},
            'status': {'nodeInfo': {'kubeletVersion': 'v1.35.0'},
                       'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    pod = {'metadata': {'namespace': 'default', 'name': 'redis-0', 'creationTimestamp': now.isoformat()},
           'spec': {'volumes': [{'persistentVolumeClaim': {'claimName': 'data'}}]},
           'status': {'phase': 'Pending'}}
    pv = {'metadata': {'name': 'disk'}, 'status': {'phase': 'Bound'}, 'spec': {'nodeAffinity': {
        'required': {'nodeSelectorTerms': [{'matchExpressions': [
            {'key': gate.ZONE_KEYS[0], 'operator': 'In', 'values': ['zone-c']}]}]}}}}
    data = {'nodes': {'items': [node]}, 'pods': {'items': [pod]}, 'events': {'items': []},
            'pvc': {'items': [{'metadata': {'namespace': 'default', 'name': 'data'},
                               'spec': {'volumeName': 'disk'}}]}, 'pv': {'items': [pv]}}
    calls, failures = [], set()
    def run(args, **kwargs):
        calls.append(args)
        return reply(args, data[args[2]], int(args[2] in failures))
    monkeypatch.setattr(gate, 'run_cmd', run)
    monkeypatch.setattr(gate, 'mng_rollout_in_progress', lambda *a: False)
    return audit, data, calls, failures, now


def test_young_pending_pvc_without_ready_zone_stops(zonal, capsys):
    audit, _, calls, _, _ = zonal
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 1
    assert 'PV-AZ pod=default/redis-0 PV=disk zone=zone-c' in audit.read_text()
    assert capsys.readouterr().out.splitlines()[-1] == (
        'NEXT: STOP no Ready node in zone zone-c for PV-bound Pods; add a node in zone-c '
        '(node group desired +1), wait for the Pods, then restore desired')
    assert ['kubectl', 'get', 'pvc', '-A', '-o', 'json'] in calls
    assert ['kubectl', 'get', 'pv', '-o', 'json'] in calls


@pytest.mark.parametrize('label', gate.ZONE_KEYS)
@pytest.mark.parametrize('available', [True, False])
def test_pv_zone_ready_schedulable_node_required(zonal, capsys, label, available):
    audit, data, _, _, _ = zonal
    data['pv']['items'][0]['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['key'] = label
    data['nodes']['items'][0]['metadata']['labels'] = {label: 'zone-c'}
    data['nodes']['items'][0]['spec'] = {'unschedulable': not available}
    assert gate.gate_phase4('demo', '1.35', str(audit)) == (0 if available else 1)
    assert ('PV-AZ ' in audit.read_text()) != available
    if available:
        assert 'unhealthy Pod 없음' in audit.read_text()


@pytest.mark.parametrize('age,expected', [(30, 2), (1801, 1)])
def test_pv_gap_rollout_retries_only_within_limit(zonal, monkeypatch, capsys, age, expected):
    audit, _, _, _, now = zonal
    audit.write_text(f'{(now-timedelta(days=2)).isoformat()} | PHASE4-DATAPLANE | FAIL | old target\n'
                     f'{(now-timedelta(seconds=age)).isoformat()} | APPLY-GUARD | - | PASS | current\n')
    monkeypatch.setattr(gate, 'mng_rollout_in_progress', lambda *a: True)
    assert gate.gate_phase4('demo', '1.35', str(audit)) == expected
    output = capsys.readouterr().out.splitlines()[-1]
    assert output.startswith('NEXT: RETRY wait 30s' if expected == 2 else 'NEXT: STOP no Ready node')


@pytest.mark.parametrize('resource', ['pvc', 'pv'])
def test_storage_kubectl_failure_stops(zonal, capsys, resource):
    audit, _, _, failures, _ = zonal
    failures.add(resource)
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 1
    assert f'kubectl get {resource} 실패' in audit.read_text()


def test_non_pvc_pending_never_fetches_storage(zonal):
    audit, data, calls, _, _ = zonal
    data['pods']['items'][0]['spec']['volumes'] = []
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 0
    assert not any(c[2] in ('pv', 'pvc') for c in calls)


@pytest.mark.parametrize('prefix', ['CONFLICT vpc-cni: fields', 'PV-AZ pod=ns/p PV=disk zone=zone-c'])
def test_old_failures_do_not_override_current_stop(tmp_path, monkeypatch, capsys, prefix):
    audit = tmp_path / 'audit.log'
    audit.write_text(f'2020-01-01T00:00:00Z | PHASE4-DATAPLANE | FAIL | {prefix}\n')
    monkeypatch.setattr(gate, 'run_cmd', lambda args, **kw: reply(args, code=1))
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 1
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: STOP gate failed; resolve FAIL records before retrying'


def test_phase7_inherits_pv_az_stop(zonal, monkeypatch, capsys):
    audit, _, _, _, _ = zonal
    monkeypatch.setattr(gate, 'gate_phase2', lambda *a: 0)
    monkeypatch.setattr(gate, 'gate_phase3', lambda *a: 0)
    original = gate.run_cmd
    monkeypatch.setattr(gate, 'run_cmd', lambda args, **kw:
                        reply(args, {'insights': []}) if args[0] == 'aws' else original(args, **kw))
    assert gate.gate_phase7('demo', '1.35', str(audit)) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith('NEXT: STOP no Ready node in zone zone-c')


def test_phase7_inherits_conflict_stop(tmp_path, monkeypatch, capsys):
    def conflict(cluster, audit):
        gate.audit_append(audit, 'PHASE3-ADDON', 'FAIL', 'CONFLICT vpc-cni: field conflict')
        return 1
    monkeypatch.setattr(gate, 'gate_phase2', lambda *a: 0)
    monkeypatch.setattr(gate, 'gate_phase3', conflict)
    monkeypatch.setattr(gate, 'gate_phase4', lambda *a, **kw: 0)
    monkeypatch.setattr(gate, 'run_cmd', lambda args, **kw: reply(args, {'insights': []}))
    assert gate.gate_phase7('demo', '1.35', str(tmp_path / 'audit.log')) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith('NEXT: STOP add-on ConfigurationConflict (vpc-cni)')


def test_pv_rollout_does_not_hide_other_blocking_pods(zonal, monkeypatch, capsys):
    audit, data, _, _, now = zonal
    audit.write_text(f'{now.isoformat()} | APPLY-GUARD | - | PASS | current\n')
    data['pods']['items'].append({'metadata': {'namespace': 'default', 'name': 'other',
        'creationTimestamp': (now-timedelta(minutes=10)).isoformat()}, 'status': {'phase': 'Pending'}})
    monkeypatch.setattr(gate, 'mng_rollout_in_progress', lambda *a: True)
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 1
    assert 'ROLLOUT PV-AZ' in audit.read_text() and 'BLOCKING Pod' in audit.read_text()
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: STOP gate failed; resolve FAIL records before retrying'


def test_pv_alternative_zone_with_ready_node_is_not_gap(zonal):
    audit, data, _, _, _ = zonal
    expression = data['pv']['items'][0]['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]
    expression['values'].append('zone-a')
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 0
    assert 'PV-AZ' not in audit.read_text()


def test_fargate_node_in_pv_zone_does_not_close_gap(zonal):
    audit, data, _, _, _ = zonal
    data['nodes']['items'].append({'metadata': {'name': 'fargate-ip', 'labels': {
        'topology.kubernetes.io/zone': 'zone-c', 'eks.amazonaws.com/compute-type': 'fargate'}},
        'status': {'nodeInfo': {'kubeletVersion': 'v1.35.0'},
                   'conditions': [{'type': 'Ready', 'status': 'True'}]}})
    assert gate.gate_phase4('demo', '1.35', str(audit)) == 1
    assert 'PV-AZ pod=default/redis-0 PV=disk zone=zone-c' in audit.read_text()


@pytest.fixture(autouse=True)
def empty_statefulsets_for_existing_scenarios(monkeypatch):
    # Existing scenarios have no StatefulSets; the new rollout query must stay offline.
    monkeypatch.setattr(gate, "kubectl_json", lambda *args, **kwargs: {"items": []})
