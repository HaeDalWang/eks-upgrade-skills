"""Offline evidence for E5 diagnostics; no cluster or Terraform process is started."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import discover
import drain_watch
import gate_check
import lib
import phase_gate
import plan_check


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    lib.reset_gate()
    monkeypatch.setattr(lib, 'skill_revision', lambda: 'test-revision')
    monkeypatch.setattr(drain_watch, '_kubectl_get', lambda *args: {'items': []})


def sts(name='db', ready=0, replicas=2, current='old', update='new'):
    return {'metadata': {'namespace': 'app', 'name': name},
            'spec': {'replicas': replicas}, 'status': {'currentRevision': current,
            'updateRevision': update, 'readyReplicas': ready}}


def pod(kind='StatefulSet', name='db-0', uid='pod-uid'):
    return {'metadata': {'namespace': 'app', 'name': name, 'uid': uid,
            'creationTimestamp': '2020-01-01T00:00:00Z',
            'ownerReferences': [{'kind': kind, 'name': 'db', 'controller': True}]},
            'spec': {'nodeName': 'worker'}, 'status': {'phase': 'Pending'}}


def node(**spec):
    return {'metadata': {'name': 'worker'}, 'spec': spec,
            'status': {'nodeInfo': {'kubeletVersion': 'v1.34.0'},
                       'conditions': [{'type': 'Ready', 'status': 'True'}]}}


def test_s5_pvc_az_targets_are_written_to_audit(monkeypatch, tmp_path):
    pv = {'metadata': {'name': 'volume'}, 'status': {'phase': 'Bound'},
          'spec': {'claimRef': {'namespace': 'app', 'name': 'data'},
                   'nodeAffinity': {'required': {'nodeSelectorTerms': [
                       {'matchExpressions': [{'key': 'topology.kubernetes.io/zone', 'values': ['zone-a']}]}]}}}}
    monkeypatch.setattr(gate_check, 'kubectl_json', lambda resource, **kw:
                        {'items': [pv]} if resource == 'pv' else {'items': []})
    gate_check.check_wls003()
    log = tmp_path / 'audit'
    lib.audit_flush(str(log))
    assert 'app/data@zone-a' in log.read_text()


def test_s5_topology_targets_include_kind_and_truncate(monkeypatch, tmp_path):
    def get(resource, **kwargs):
        if resource == 'nodes':
            return {'items': []}
        return {'items': [{'metadata': {'namespace': 'app', 'name': 'db' + str(i)},
                           'spec': {'template': {'spec': {'topologySpreadConstraints': [
                               {'whenUnsatisfiable': 'DoNotSchedule'}]}}}}
                          for i in range(lib.DIAGNOSTIC_TARGET_LIMIT + 1)]}
    monkeypatch.setattr(gate_check, 'kubectl_json', get)
    gate_check.check_wls006()
    log = tmp_path / 'audit'
    lib.audit_flush(str(log))
    text = log.read_text()
    assert 'app/Deployment/db0' in text
    assert '외 12개' in text  # 11 Deployments + 11 StatefulSets, limit 10
    assert 'app/StatefulSet/db10' not in text


@pytest.mark.parametrize('samples,threshold,fail_count,warn_count', [
    ([(0, True), (599, True), (600, True), (630, True)], 600, 1, 1),
    ([(0, True), (300, None), (600, True)], 600, 1, 1),
    ([(0, True), (599, False), (600, True), (1199, True)], 600, 0, 2),
    ([(0, True), (600, True), (630, False), (660, True), (1260, True)], 600, 2, 2),
    ([(0, True), (59, True), (60, True)], 60, 1, 1),
])
def test_s11_sts_warn_then_sustained_fail_and_episode_reset(samples, threshold, fail_count,
                                                         warn_count, tmp_path):
    ticks = iter([0] + [now for now, _ in samples] + [10000])
    values = iter([None if bad is None else {'items': [sts()] if bad else []}
                   for _, bad in samples])
    log = tmp_path / 'audit'
    code = drain_watch.watch_loop('P4', str(log), 30, 10000, 'all',
        poll_events=lambda: {'items': []}, poll_pdb=lambda: {'items': []},
        poll_nc=lambda: {'items': []}, poll_sts=lambda: next(values),
        sts_fail_after=threshold, clock=lambda: next(ticks), sleep_fn=lambda _: None)
    rows = [lib.parse_audit_line(line) for line in log.read_text().splitlines()]
    signals = [r for r in rows if r and r['detail'].startswith('StatefulSetStalled:')]
    assert sum(r['result'] == 'FAIL' for r in signals) == fail_count
    assert sum(r['result'] == 'WARN' for r in signals) == warn_count
    assert code == bool(fail_count)
    assert all('kubectl delete pod' in row['detail'] for row in signals)


def test_s11_sts_conditions_and_independent_timers():
    data = {'items': [sts(), sts('other'), sts('ready', ready=2), sts('same', update='old'),
                      sts('zero', replicas=0)]}
    original = copy.deepcopy(data)
    signals, state = drain_watch.extract_stalled_statefulsets(data, {'app/db': (0, False)}, 600, 600)
    assert [s['severity'] for s in signals] == ['FAIL', 'WARN']
    assert state['app/other'] == (600, False)
    assert data == original


@pytest.mark.parametrize('baseline,known', [('match', True), ('other-target', False),
                                          ('new-run', False), ('forged', False)])
def test_s11_phase4_warn_next_and_cumulative_baseline(baseline, known, tmp_path, monkeypatch, capsys):
    log = tmp_path / 'audit'
    def seed(version, workloads):
        lib.audit_append(str(log), 'BASELINE-PODS', 'INFO',
            f'cluster_name=demo target_version={version} workloads={workloads}')
    seed('1.33', 'app/StatefulSet/db')  # previous upgrade
    seed('1.34', 'app/StatefulSet/db' if baseline not in ('other-target', 'forged') else '')
    if baseline == 'new-run':
        seed('1.34', '')  # latest run invalidates prior same-target evidence
    if baseline == 'forged':
        result = subprocess.run([sys.executable, str(Path(lib.__file__).with_name('audit_event.py')),
            '--audit-log', str(log), '--rule-id', 'BASELINE-PODS', '--result', 'INFO',
            '--detail', 'cluster_name=demo target_version=1.34 workloads=app/StatefulSet/db'],
            capture_output=True, text=True)
        assert result.returncode == 1 and 'reserved' in result.stderr
    seed('1.35', '')  # later unrelated target must not shadow current target
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: {'items': [sts()]})
    def run(cmd, **kw):
        data = {'nodes': [node()], 'pods': [pod()], 'events': []}[cmd[2]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == (2 if known else 1)
    new = log.read_text().split('# K8s Upgrade Pre-flight Audit Log')[-1]
    assert ('baseline 기존 장애' in new) == known
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith('NEXT: ASK_USER' if known else 'NEXT: STOP')
    assert 'app/StatefulSet/db' in last and 'kubectl delete pod db-0 -n app' in last


def test_s11_phase4_sts_lookup_failure_records_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: None)
    assert phase_gate.check_stalled_statefulsets(str(tmp_path / 'audit'), 'demo', '1.34', {'items': []}) == (0, [])
    assert 'unknown' in lib._gate.audit_lines[-1]


def test_s11_sts_cli_threshold_default_and_override(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/bin/kubectl')
    calls = []
    monkeypatch.setattr(drain_watch, 'watch_loop', lambda **kwargs: calls.append(kwargs) or 0)
    for flags, seconds in [([], 600), (['--sts-fail-after', '60'], 60)]:
        monkeypatch.setattr('sys.argv', ['drain_watch.py', '--phase', 'P4', '--audit-log', 'audit'] + flags)
        assert drain_watch.main() == 0
        assert calls[-1]['sts_fail_after'] == seconds


def test_s12_masks_nested_sensitive_values_and_secret_keys_without_mutation():
    data = {'values': {'plain': 'keep', 'credential': [{'value': 'hidden'}]},
            'sensitive_values': {'credential': [{'value': True}]},
            'change': {'before': {'opaque': 'old-hidden'}, 'before_sensitive': {'opaque': True},
                       'after': {'opaque': 'new-hidden'}, 'after_sensitive': {'opaque': True}},
            'db_password': 'password-value', 'auth_token': 'token-value',
            'private_key': 'key-value', 'client_secret': 'secret-value', 'text': '(sensitive value)'}
    original = copy.deepcopy(data)
    safe = lib.redact_plan(data)
    text = json.dumps(safe)
    for value in ['hidden', 'old-hidden', 'new-hidden', 'password-value', 'key-value', 'secret-value']:
        assert value not in text
    assert safe['auth_token'] == 'token-value'
    assert safe['values']['plain'] == 'keep'
    assert data == original


def test_s12_discovery_masks_derived_sensitive_values_even_on_halt(tmp_path, monkeypatch):
    args = SimpleNamespace(auth_prefix='', tf_var_file='', cluster_name='', output=str(tmp_path / 'discovery.json'),
                           audit_log=str(tmp_path / 'audit'))
    plan = {'resource_changes': [{'change': {'after': {'opaque': 'unique-hidden'},
                                            'after_sensitive': {'opaque': True}}}]}
    monkeypatch.setattr(discover, 'resolve_tf_dir', lambda _: tmp_path)
    monkeypatch.setattr(discover, 'trial_plan', lambda *a: plan)
    def analyze(args, doc, safe_plan, tf_dir):
        value = safe_plan['resource_changes'][0]['change']['after']['opaque']
        doc.update(module={'address': 'module.eks'}, cluster_version={'value': value})
        raise discover.Halt('stop', 'terraform_plan', 'invalid ' + value)
    monkeypatch.setattr(discover, 'analyze_terraform', analyze)
    doc = dict(status=None, unknowns=[], cluster_name='demo', tf_dir=None, module=None, node_pools=[])
    with pytest.raises(discover.Halt) as exc:
        discover.discover(args, doc)
    doc['status'] = 'stop'
    doc['unknowns'].append({'field': 'terraform_plan', 'reason': exc.value.reason, 'stop': True})
    assert discover.finish(args, doc) == 1
    persisted = (tmp_path / 'discovery.json').read_text() + (tmp_path / 'audit').read_text()
    assert 'unique-hidden' not in persisted and 'inline-hidden' not in persisted
    assert '***' in persisted


def test_f3_unstructured_error_diagnostics_are_preserved():
    error = discover.classify_failure('Error: secret "a" not found')
    assert 'secret "a" not found' in error.reason


def test_s12_node_table_masks_sensitive_ami(capsys):
    resource = {'address': 'aws_eks_node_group.worker', 'type': 'aws_eks_node_group',
                'change': {'actions': ['update'], 'before': {'release_version': 'old'},
                           'after': {'release_version': 'private-ami'},
                           'after_sensitive': {'release_version': True}}}
    plan_check._print_node_table([resource], {'release_version': 'private-ami'})
    output = capsys.readouterr().out
    assert 'private-ami' not in output and '***' in output


@pytest.mark.parametrize('retiring', ['cordon', 'deleting', 'healthy'])
@pytest.mark.parametrize('kind', ['DaemonSet', 'Deployment'])
def test_s14_only_retiring_node_daemonset_scheduling_events_suppressed(retiring, kind):
    worker = node(unschedulable=retiring == 'cordon')
    if retiring == 'deleting':
        worker['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    p = pod(kind)
    suppressed = drain_watch.suppressed_daemonset_pods({'items': [p]}, {'items': [worker]})
    events = {'items': [{'metadata': {'uid': 'event', 'namespace': 'app'}, 'type': 'Warning',
                         'reason': 'FailedMount', 'involvedObject': {'kind': 'Pod',
                          'namespace': 'app', 'name': 'db-0', 'uid': 'pod-uid'}}]}
    new, _ = drain_watch.extract_warning_events(events, set(), suppressed)
    assert bool(new) == (kind != 'DaemonSet' or retiring == 'healthy')
    events['items'][0]['reason'] = 'OOMKilling'
    assert drain_watch.extract_warning_events(events, set(), suppressed)[0]
    events['items'][0]['reason'] = 'FailedMount'
    events['items'][0]['involvedObject']['uid'] = 'replacement-uid'
    assert drain_watch.extract_warning_events(events, set(), suppressed)[0]


def test_f3_structural_masking_preserves_equal_values_and_substrings():
    plan = {'configuration': {'root_module': {'variables': {'opaque': {'sensitive': True}}}},
            'variables': {'opaque': {'value': 'required'}},
            'resource_changes': [{'change': {
                'after': {'credential': 'required', 'http_tokens': 'required',
                          'db_password': 'hidden', 'passwordless': 'keep',
                          'version': '1.34', 'port': 5432,
                          'text': 'required 1.34 port 5432', 'short': '1'},
                'after_sensitive': {'credential': True, 'short': True}}}]}
    original = copy.deepcopy(plan)
    safe = lib.redact_plan_output(plan)
    after = safe['resource_changes'][0]['change']['after']
    assert safe['variables']['opaque']['value'] == '***'
    assert after == {'credential': '***', 'http_tokens': 'required',
                     'db_password': '***', 'passwordless': 'keep', 'version': '1.34',
                     'port': 5432, 'text': 'required 1.34 port 5432', 'short': '***'}
    assert plan == original


@pytest.mark.parametrize('result', [0, 1])
def test_f3_phase6_preserves_diagnostics_and_removes_temp_plan(result, tmp_path, monkeypatch, capsys):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if 'plan' in cmd:
            if result:
                return subprocess.CompletedProcess(cmd, 1, '', 'password="multi word hidden"')
            return subprocess.CompletedProcess(cmd, 0, '', '')
        data = {'resource_changes': [{'address': 'aws_eks_node_group.group["hidden-index"]',
                'type': 'aws_eks_node_group', 'change': {'actions': ['delete', 'create'],
                'after': {'opaque': 'hidden-index'}, 'after_sensitive': {'opaque': True}}}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')
    monkeypatch.setattr(subprocess, 'run', run)
    audit = tmp_path / 'audit'
    assert phase_gate.gate_phase6(str(tmp_path), str(audit)) == 1
    persisted = audit.read_text() + capsys.readouterr().out
    assert ('multi word hidden' if result else 'hidden-index') in persisted
    assert not list(tmp_path.glob('*.tfplan'))


def test_f3_plan_check_preserves_resource_address(tmp_path, monkeypatch, capsys):
    from test_plan_check import invoke, plan, resource
    change = resource('aws_security_group', {}, {'opaque': 'hidden-index'})
    change['address'] = 'aws_security_group.group["hidden-index"]'
    change['change']['after_sensitive'] = {'opaque': True}
    code, saved, audit, _ = invoke(tmp_path, monkeypatch, plan(change))
    assert code == 2
    assert 'hidden-index' in audit.read_text() + capsys.readouterr().out
    assert saved.read_bytes() == b'saved terraform plan'


@pytest.mark.parametrize('detail', ['secret "a" not found',
    'aws-secrets-store-csi-driver-provider: FailedMount', 'password="hidden value"'])
def test_f2_shared_audit_sink_preserves_diagnostics(tmp_path, detail):
    log = tmp_path / 'audit'
    lib.audit_append(str(log), 'USER-APPLY', 'FAIL', detail)
    lib.audit_write('PHASE6-TFSYNC', 'FAIL', detail)
    lib.audit_flush(str(log))
    rows = [lib.parse_audit_line(line) for line in log.read_text().splitlines()]
    assert [r['detail'] for r in rows if r] == [detail, detail]


def test_s14_pending_daemonset_affinity_target_is_suppressed():
    p = pod('DaemonSet')
    p['spec'] = {'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
        'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': ['worker']}]}]}}}}
    assert drain_watch.suppressed_daemonset_pods({'items': [p]}, {'items': [node(unschedulable=True)]})
    p['spec']['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'].append({})
    assert not drain_watch.suppressed_daemonset_pods({'items': [p]}, {'items': [node(unschedulable=True)]})


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_s14_watch_loop_applies_node_filter_and_keeps_workload_events(tmp_path, lookup_fails):
    ds, ordinary = pod('DaemonSet'), pod('Deployment', 'api-0', 'api-uid')
    events = {'items': [{'metadata': {'uid': p['metadata']['uid'], 'namespace': 'app'}, 'type': 'Warning',
                         'reason': 'FailedMount', 'involvedObject': {'kind': 'Pod',
                         'namespace': 'app', 'name': p['metadata']['name'], 'uid': p['metadata']['uid']}}
                        for p in (ds, ordinary)]}
    ticks = iter([0, 0, 1000])
    log = tmp_path / 'audit'
    assert drain_watch.watch_loop('P4', str(log), 30, 1000, 'all',
        poll_events=lambda: events, poll_pdb=lambda: {'items': []}, poll_nc=lambda: {'items': []},
        poll_sts=lambda: {'items': []}, poll_pods=lambda: {'items': [ds, ordinary]},
        poll_nodes=lambda: None if lookup_fails else {'items': [node(unschedulable=True)]},
        clock=lambda: next(ticks), sleep_fn=lambda _: None) == 0
    assert 'FailedMount: app/api-0' in log.read_text()
    assert ('FailedMount: app/db-0' in log.read_text()) == lookup_fails


def test_f2_quoted_audit_detail_is_unchanged(tmp_path):
    from upgrade_plan import detail_fields, fields
    log = tmp_path / 'audit'
    detail = detail_fields(detail='secret "a" not found', step='cp')
    lib.audit_append(str(log), 'USER-APPLY', 'FAIL', detail)
    row = lib.parse_audit_line(log.read_text())
    assert row['detail'] == detail
    assert fields(row['detail']) == {'detail': 'secret "a" not found', 'step': 'cp'}


def test_s11_stalled_sts_warning_does_not_waive_unrelated_blocking_pod(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: {'items': [sts()]})
    def run(cmd, **kw):
        data = {'nodes': [node()], 'pods': [pod(), pod('Deployment', 'other-0')], 'events': []}[cmd[2]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    log = tmp_path / 'audit'
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == 1
    assert 'StatefulSetStalled' in log.read_text() and 'BLOCKING Pod 2개: app/db-0, app/other-0' in log.read_text()
    assert capsys.readouterr().out.splitlines()[-1].startswith('NEXT: STOP')


def test_f1_stalled_sts_twenty_minute_crashloop_stays_blocking(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    broken = pod()
    broken['metadata']['creationTimestamp'] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    broken['status'] = {'phase': 'Running', 'containerStatuses': [
        {'ready': False, 'state': {'waiting': {'reason': 'CrashLoopBackOff'}}}]}
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: {'items': [sts()]})
    def run(cmd, **kw):
        data = {'nodes': [node()], 'pods': [broken], 'events': []}[cmd[2]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    log = tmp_path / 'audit'
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == 1
    rows = [lib.parse_audit_line(line) for line in log.read_text().splitlines()]
    failure = next(r for r in rows if r and r['detail'].startswith('BLOCKING Pod'))
    assert failure['result'] == 'FAIL'
    assert 'kubectl delete pod db-0 -n app' in failure['detail']
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith('NEXT: STOP') and 'kubectl delete pod db-0 -n app' in last


def test_f5_transient_sts_and_stale_next_keeps_both_actions(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone
    transient, stale = pod(), pod('Deployment', 'old-0')
    transient['metadata']['creationTimestamp'] = datetime.now(timezone.utc).isoformat()
    transient['status'] = {'phase': 'Running', 'containerStatuses': [{'ready': False}]}
    stale['status']['phase'] = 'Failed'
    stale['metadata']['ownerReferences'][0]['uid'] = 'owner-uid'
    sibling = copy.deepcopy(stale)
    sibling['metadata']['name'] = 'ready-0'
    sibling['status'] = {'phase': 'Running'}
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: {'items': [sts()]})
    def run(cmd, **kw):
        data = {'nodes': [node()], 'pods': [transient, stale, sibling], 'events': []}[cmd[2]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    assert phase_gate.gate_phase4('demo', '1.34', str(tmp_path / 'audit')) == 2
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith('NEXT: RUN delete-stale-pods app/old-0; ASK_USER ')
    assert 'kubectl delete pod db-0 -n app' in last


def test_f4_scheduling_reasons_are_not_added_to_watch_scope():
    assert 'FailedScheduling' not in drain_watch.WATCHED_REASONS
    assert 'Pending' not in drain_watch.WATCHED_REASONS
    for reason in ('FailedScheduling', 'Pending'):
        assert not drain_watch.extract_warning_events({'items': [
            {'type': 'Warning', 'reason': reason}]}, set())[0]


def test_f3_parent_sensitive_marker_cannot_be_overridden_by_resource_attribute():
    plan = {'values': {'nested': {'value': 'hidden', 'sensitive': False}},
            'sensitive_values': {'nested': {'value': True}}}
    assert lib.redact_plan_output(plan)['values']['nested'] == {'value': '***', 'sensitive': False}


@pytest.mark.parametrize('rule_id,detail', [
    ('PHASE4-DATAPLANE', 'ROLLOUT nodes are updating'),
    ('PHASE3-ADDON', 'UPDATING addon'),
    ('PHASE4-DATAPLANE', 'TRANSIENT Pod app/db-0'),
])
def test_redo2_stalled_sts_keeps_retry_priority(rule_id, detail):
    rows = [{'rule_id': 'PHASE4-DATAPLANE', 'result': 'WARN',
             'detail': 'StatefulSetStalled: app/StatefulSet/db; inspect Pods'},
            {'rule_id': rule_id, 'result': 'WARN', 'detail': detail}]
    action = phase_gate.gate_next(2, rows)
    assert action.startswith('NEXT: RETRY ')
    assert 'StatefulSetStalled: app/StatefulSet/db' in action


def test_redo2_sts_lookup_failure_does_not_change_passing_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(phase_gate, 'kubectl_json', lambda *a, **kw: None)
    def run(cmd, **kw):
        data = {'nodes': [node()], 'pods': [], 'events': []}[cmd[2]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'items': data}), '')
    monkeypatch.setattr(phase_gate, 'run_cmd', run)
    log = tmp_path / 'audit'
    assert phase_gate.gate_phase4('demo', '1.34', str(log)) == 0
    rows = [lib.parse_audit_line(line) for line in log.read_text().splitlines()]
    unknown = next(r for r in rows if r and 'StatefulSet 조회 unknown' in r['detail'])
    assert unknown['result'] == 'INFO'
    assert 'kubectl get statefulsets 실패' in unknown['detail']


def test_redo2_suppressed_event_stays_seen_after_node_and_pod_disappear(tmp_path):
    ds = pod('DaemonSet')
    event = {'metadata': {'uid': 'event', 'namespace': 'app'}, 'type': 'Warning',
             'reason': 'FailedMount', 'involvedObject': {'kind': 'Pod',
             'namespace': 'app', 'name': 'db-0', 'uid': 'pod-uid'}}
    ticks = iter([0, 0, 30, 60])
    pods = iter([{'items': [ds]}, {'items': []}])
    nodes = iter([{'items': [node(unschedulable=True)]}, {'items': []}])
    log = tmp_path / 'audit'
    assert drain_watch.watch_loop('P4', str(log), 30, 60, 'all',
        poll_events=lambda: {'items': [event]}, poll_pdb=lambda: {'items': []},
        poll_nc=lambda: {'items': []}, poll_sts=lambda: {'items': []},
        poll_pods=lambda: next(pods), poll_nodes=lambda: next(nodes),
        clock=lambda: next(ticks), sleep_fn=lambda _: None) == 0
    assert 'FailedMount:' not in log.read_text()


def test_redo2_next_deduplicates_guidance_by_sts_identity():
    first = 'StatefulSetStalled: app/StatefulSet/db; inspect first Pod'
    other = 'StatefulSetStalled: other/StatefulSet/db; inspect other Pod'
    repeated = 'StatefulSetStalled: app/StatefulSet/db; inspect changed Pod'
    rows = [{'rule_id': 'PHASE4-DATAPLANE', 'result': result, 'detail': detail}
            for result, detail in [('WARN', first), ('WARN', other),
                                   ('FAIL', 'BLOCKING Pod app/db-0; ' + repeated + '; ' + other)]]
    action = phase_gate.gate_next(1, rows)
    assert action.count('StatefulSetStalled: app/StatefulSet/db;') == 1
    assert action.count('StatefulSetStalled: other/StatefulSet/db;') == 1


def test_redo2_sts_first_warning_omits_elapsed_time(tmp_path):
    ticks = iter([0, 0, 600, 630])
    log = tmp_path / 'audit'
    assert drain_watch.watch_loop('P4', str(log), 30, 630, 'all',
        poll_events=lambda: {'items': []}, poll_pdb=lambda: {'items': []},
        poll_nc=lambda: {'items': []}, poll_sts=lambda: {'items': [sts()]},
        clock=lambda: next(ticks), sleep_fn=lambda _: None) == 1
    rows = [lib.parse_audit_line(line) for line in log.read_text().splitlines()]
    signals = [r for r in rows if r and r['detail'].startswith('StatefulSetStalled:')]
    assert [r['result'] for r in signals] == ['WARN', 'FAIL']
    assert 'for ' not in signals[0]['detail']
    assert 'for 600s' in signals[1]['detail']
