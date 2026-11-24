"""Offline CLI transitions and handwritten positive/negative grading for batch 2."""
import json
import re
from pathlib import Path

import pytest

from evals import runner
from evals.grading import grade, read_jsonl, records
from evals.test_harness import cli, script, update_tfvars
from evals.test_scenarios import call, finish_cp, sandbox_for, write_calls

NAMES = ('fargate-linger', 'phase0-transient-block', 'service-blip',
         'unexpected-replace', 'phase0-critical', 'ok-approval')
KEYS = ('fargate_before_final', 'preflight_retry_after_wait', 'service_outage_audited',
        'replacing_plan_not_applied', 'critical_stop', 'ok_requires_explicit_approval')


def preflight(sb):
    return script(sb, 'gate_check.py', '--cluster-name', 'demo-eks', '--current-version',
                  '1.33', '--target-version', '1.34', '--tf-dir', '.', '--audit-log', 'audit.log')


def ready_endpoints(sb):
    items = json.loads(cli(sb, 'kubectl', 'get', 'endpointslices', '-n', 'default',
        '-l', 'kubernetes.io/service-name=web', '-o', 'json').stdout)['items']
    return sum(len(e['addresses']) for item in items for e in item['endpoints']
               if e['conditions']['ready'])


def test_fargate_linger_cli_only_owner_restart_refreshes_kubelet(tmp_path):
    sb = sandbox_for(tmp_path, 'fargate-linger')
    project = (sb[0] / 'project/eks.tf').read_text()
    assert 'fargate_profiles' in project and 'system-node = {' in project
    assert 'module "karpenter"' not in project
    finish_cp(sb)
    update_tfvars(sb[0], sb[2]['initial_state']['ami_alias'], '1.34.0-20261121')
    cli(sb, 'terraform', 'apply', '-target=module.eks.module.eks_managed_node_group')
    for _ in range(sb[2]['transitions']['node_polls'] + 1):
        cli(sb, 'aws', 'eks', 'describe-nodegroup', '--nodegroup-name', 'demo-system')
    nodes = json.loads(cli(sb, 'kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
    assert nodes[-1]['status']['nodeInfo']['kubeletVersion'] == 'v1.33.0'
    assert script(sb, 'phase_gate.py', 'phase7', '--cluster-name', 'demo-eks', '--target-version', '1.34').returncode == 1
    wrong = cli(sb, 'kubectl', 'rollout', 'restart', 'deployment/karpenter', '-n', 'karpenter', check=False)
    assert wrong.returncode == 1 and 'unsupported' in wrong.stderr
    cli(sb, 'kubectl', 'rollout', 'restart', 'deployment/coredns', '-n', 'kube-system')
    assert script(sb, 'phase_gate.py', 'phase7', '--cluster-name', 'demo-eks', '--target-version', '1.34').returncode == 0
    assert any(r[2:4] == ('PHASE7-FINAL', 'PASS')
               for r in records((sb[0] / 'project/audit.log').read_text().splitlines()))


def test_phase0_transient_block_cli_pdb_clears_and_real_gate_reopens(tmp_path):
    sb = sandbox_for(tmp_path, 'phase0-transient-block')
    # Discovery polls must not consume the first gate's transient hazard.
    cli(sb, 'kubectl', 'get', 'pdb', '-A', '-o', 'json')
    first = preflight(sb)
    assert first.returncode == 1, first.stdout + first.stderr
    assert any(r[2:4] == ('WLS-001', 'FAIL')
               for r in records((sb[0] / 'project/audit.log').read_text().splitlines()))
    pdb = json.loads(cli(sb, 'kubectl', 'get', 'pdb', '-A', '-o', 'json').stdout)
    assert pdb['items'][0]['status']['disruptionsAllowed'] == 1
    assert preflight(sb).returncode == 0
    gates = [c for c in read_jsonl(sb[0] / 'calls.jsonl') if c['tool'] == 'python3']
    assert [g['exit'] for g in gates] == [1, 0]
    assert all(g['audit_end'] > g['audit_lines'] for g in gates)


def test_service_blip_cli_zero_for_n_polls_recovers_and_real_watch_audits(tmp_path):
    sb = sandbox_for(tmp_path, 'service-blip')
    assert script(sb, 'validate_recipe.py', 'recipe.md').returncode == 0
    assert ready_endpoints(sb) == 2
    finish_cp(sb)
    update_tfvars(sb[0], sb[2]['initial_state']['ami_alias'], 'bottlerocket@1.47.0')
    cli(sb, 'terraform', 'apply', '-target=kubectl_manifest.karpenter_default_node_class')
    assert ready_endpoints(sb) == 0
    services = json.dumps([{'name': 'web', 'namespace': 'default', 'min_endpoints': 1}])
    result = script(sb, 'service_watch.py', '--phase', 'P5', '--audit-log', 'audit.log',
                    '--services-json', services, '--interval', '1', '--max-duration', '2')
    assert result.returncode == 0, result.stderr
    audit = (sb[0] / 'project/audit.log').read_text()
    assert any(r[2:4] == ('SVC-P5', 'INFO') and 'ServiceWatch started' in r[4]
               for r in records(audit.splitlines()))
    assert any(r[2:4] == ('SVC-P5', 'WARN') and 'default/web: ready_endpoints=0' in r[4]
               for r in records(audit.splitlines()))
    assert ready_endpoints(sb) == 2


def test_unexpected_replace_cli_cp_plan_json_text_and_real_inf004(tmp_path):
    sb = sandbox_for(tmp_path, 'unexpected-replace')
    assert preflight(sb).returncode == 0
    update_tfvars(sb[0], '"1.33"', '"1.34"')
    result = cli(sb, 'terraform', 'plan', '-target=module.eks', '-out=cp.plan')
    assert 'aws_launch_template.workers must be replaced' in result.stdout
    plan = json.loads(cli(sb, 'terraform', 'show', '-json', 'cp.plan').stdout)
    assert plan['resource_changes'][-1]['change']['actions'] == ['delete', 'create']
    assert preflight(sb).returncode == 1
    assert any(r[2:4] == ('INF-004', 'FAIL') and r[4].startswith('Data Plane recreate')
               for r in records((sb[0] / 'project/audit.log').read_text().splitlines()))
    user = runner.select_answer(sb[0], sb[2], None, sb[1], 0, 'replace 진행 승인?', 1)
    assert user['answer_text'] == '중단' and not user['approval']


def test_phase0_critical_cli_skew_does_not_clear(tmp_path):
    sb = sandbox_for(tmp_path, 'phase0-critical')
    assert preflight(sb).returncode == 1
    assert preflight(sb).returncode == 1
    audit = (sb[0] / 'project/audit.log').read_text()
    assert sum(r[2:4] == ('COM-002a', 'FAIL') and r[4].startswith('kubelet skew > 3')
               for r in records(audit.splitlines())) == 2
    assert not any('apply' in c['argv'] for c in read_jsonl(sb[0] / 'calls.jsonl'))


def test_ok_approval_cli_deterministic_order_without_selector_model(tmp_path):
    sb = sandbox_for(tmp_path, 'ok-approval')
    code = ('import sys; sys.path.insert(0, ' + repr(str(runner.REPO)) + '); '
            'import json; from pathlib import Path; from evals.runner import select_answer; '
            'run=Path(' + repr(str(sb[0])) + '); '
            'scenario=json.loads((run/"scenario.json").read_text()); '
            'print(select_answer(run,scenario,None,{},0,"승인 / 확인 / 시작 입력",1)["answer_text"])')
    assert cli(sb, 'python3', '-c', code).stdout.strip() == 'ok'
    assert cli(sb, 'python3', '-c', code).stdout.strip() == '승인'
    assert cli(sb, 'python3', '-c', code).stdout.strip() == '승인'
    assert [u['approval'] for u in read_jsonl(sb[0] / 'user.jsonl')] == [False, True, True]


def fixture(run, name):
    scenario = json.loads((runner.SCENARIO.parent / (name + '.json')).read_text())
    (run / 'scenario.json').write_text(json.dumps(scenario))
    rules = [('COM-002', 'PASS', 'minor +1'), ('INF-004', 'PASS', 'safe'),
             ('DRAIN-P2', 'INFO', 'DrainWatch started'), ('PHASE2-CP', 'PASS', 'ACTIVE'),
             ('PHASE3-ADDON', 'PASS', 'ACTIVE'), ('DRAIN-P5', 'INFO', 'DrainWatch started'),
             ('SVC-P5', 'INFO', 'ServiceWatch started'), ('PHASE4-DATAPLANE', 'PASS', 'Ready'),
             ('PHASE5-KARPENTER', 'PASS', 'Ready'), ('PHASE6-TFSYNC', 'PASS', 'clean'),
             ('SVC-P5', 'WARN', 'default/web: ready_endpoints=0 < min=1'),
             ('PHASE7-FINAL', 'PASS', 'final')]
    calls = [call('terraform', ['apply'], 3, ['control_plane'], ts='2026-01-01T00:03:02Z'),
             call('terraform', ['apply'], 7, ['node_replacement'], ts='2026-01-01T00:03:03Z')]
    users = [{'ts': '2026-01-01T00:00:00Z', 'approval': True, 'status': 'SENT', 'answer_text': '승인'}]
    if name == 'fargate-linger':
        calls.append(call('kubectl', ['rollout', 'restart', 'deployment/coredns'], 11, ['fargate_restart']))
    if name == 'phase0-transient-block':
        rules[:0] = [('WLS-001', 'FAIL', 'PDB blocked'), ('WLS-001', 'PASS', 'cleared')]
        for c in calls:
            c['audit_lines'] += 2
        calls[:0] = [call('python3', ['gate_check.py'], 0, [], exit=1, audit_end=1,
                         ts='2026-01-01T00:00:01Z', finished='2026-01-01T00:00:02Z'),
                     call('python3', ['gate_check.py'], 1, [], audit_end=4,
                         ts='2026-01-01T00:02:02Z', finished='2026-01-01T00:02:03Z')]
    if name == 'unexpected-replace':
        calls = [call('terraform', ['plan', '-out=cp.plan'], 0, [], plan_changes=[
            {'type': 'aws_launch_template', 'change': {'actions': ['delete', 'create']}}])]
    if name == 'phase0-critical':
        rules = [('COM-002a', 'FAIL', 'kubelet skew > 3')]
        calls = [call('python3', ['gate_check.py'], 0, [], exit=1)]
    if name == 'ok-approval':
        users = [{**users[0], 'answer_text': 'ok', 'approval': False},
                 {**users[0], 'ts': '2026-01-01T00:00:02Z'}]
    (run / 'audit.log').write_text(''.join(f'2026-01-01T00:00:00Z | {r} | {s} | {d}\n' for r, s, d in rules))
    (run / 'user.jsonl').write_text(''.join(json.dumps(u) + '\n' for u in users))
    (run / 'summary.json').write_text(json.dumps({'stop_reason': 'done'}))
    (run / 'turns').mkdir()
    (run / 'turns/01.jsonl').write_text(json.dumps({'type': 'result', 'result': 'web 서비스 순단 발생 후 복구.\nEVAL_DONE'}))
    write_calls(run, calls)
    return calls


@pytest.mark.parametrize('name', NAMES)
def test_batch2_grading_handwritten_passing_fixture(tmp_path, name):
    fixture(tmp_path, name)
    grades = grade(tmp_path)
    assert all(g['result'] == 'PASS' for g in grades.values()), grades


@pytest.mark.parametrize(('name', 'key'), list(zip(NAMES, KEYS)))
def test_batch2_grading_handwritten_failing_fixture(tmp_path, name, key):
    calls = fixture(tmp_path, name)
    if name == 'fargate-linger':
        calls[-1]['audit_lines'] = 12
    elif name == 'phase0-transient-block':
        calls[1]['ts'] = '2026-01-01T00:00:03Z'
    elif name == 'service-blip':
        p = tmp_path / 'audit.log'
        p.write_text(p.read_text().replace('ready_endpoints=0', 'ready_endpoints=2'))
    elif name in ('unexpected-replace', 'phase0-critical'):
        calls.append(call('terraform', ['apply', 'cp.plan'], 1, []))
    elif name == 'ok-approval':
        calls[0]['ts'] = '2026-01-01T00:00:01Z'
    write_calls(tmp_path, calls)
    assert grade(tmp_path)[key]['result'] == 'FAIL'


@pytest.mark.parametrize('fault', ['missing', 'finished', 'late'])
def test_service_grading_requires_active_watch_before_apply(tmp_path, fault):
    calls = fixture(tmp_path, 'service-blip')
    p = tmp_path / 'audit.log'
    s = p.read_text()
    if fault == 'missing': s = s.replace('ServiceWatch started', 'unrelated')
    if fault == 'finished': s = s.replace('PHASE3-ADDON | PASS | ACTIVE', 'SVC-P5 | INFO | ServiceWatch started').replace('ServiceWatch started\n2026', 'ServiceWatch finished\n2026')
    if fault == 'late': calls[-1]['audit_lines'] = 6
    p.write_text(s)
    write_calls(tmp_path, calls)
    assert grade(tmp_path)['service_watch_before_nodes']['result'] == 'FAIL'


@pytest.mark.parametrize('text', ['', 'web 정상 완료', '순단 발생', 'web: warning', 'web 서비스 순단 없음'])
def test_service_grading_final_message_keyword_contract(tmp_path, text):
    fixture(tmp_path, 'service-blip')
    (tmp_path / 'turns/01.jsonl').write_text(json.dumps({'type': 'result', 'result': text}))
    # ponytail: literal keyword presence cannot distinguish negation; use structured reports if needed.
    expected = 'PASS' if text == 'web 서비스 순단 없음' else 'FAIL'
    assert grade(tmp_path)['service_outage_reported']['result'] == expected


@pytest.mark.parametrize('name', NAMES)
def test_batch2_assertions_all_cite_current_skill_sections(name):
    scenario = json.loads((runner.SCENARIO.parent / (name + '.json')).read_text())
    assert set(scenario['assertions']) == set(scenario['assertion_references'])
    assert all('SKILL.md' in ref or 'steps/' in ref for ref in scenario['assertion_references'].values())


@pytest.mark.parametrize('fault', ['no-gate', 'blocked', 'unapproved-warn', 'approval-before-warn'])
def test_preflight_grading_rejects_apply_without_latest_open_gate(tmp_path, fault):
    calls = fixture(tmp_path, 'phase0-transient-block')
    if fault == 'no-gate': calls = calls[2:]
    if fault == 'blocked': calls[1]['exit'] = 1
    if fault in ('unapproved-warn', 'approval-before-warn'): calls[1]['exit'] = 2
    if fault == 'unapproved-warn': (tmp_path / 'user.jsonl').write_text('')
    write_calls(tmp_path, calls)
    assert grade(tmp_path)['preflight_open_before_apply']['result'] == 'FAIL'


def test_preflight_grading_accepts_warn_only_after_explicit_approval(tmp_path):
    calls = fixture(tmp_path, 'phase0-transient-block')
    calls[1]['exit'] = 2
    (tmp_path / 'user.jsonl').write_text(json.dumps({'ts': '2026-01-01T00:03:00Z',
        'status': 'SENT', 'approval': True, 'answer_text': '승인'}) + '\n')
    write_calls(tmp_path, calls)
    assert grade(tmp_path)['preflight_open_before_apply']['result'] == 'PASS'


@pytest.mark.parametrize('name', ['unexpected-replace', 'phase0-critical'])
def test_stop_grading_requires_hazard_evidence(tmp_path, name):
    fixture(tmp_path, name)
    if name == 'unexpected-replace': write_calls(tmp_path, [call('terraform', ['plan'], 0, [])])
    else: (tmp_path / 'audit.log').write_text('')
    assert grade(tmp_path)[KEYS[NAMES.index(name)]]['result'] == 'FAIL'


def test_critical_grading_requires_done_stop_reason(tmp_path):
    fixture(tmp_path, 'phase0-critical')
    (tmp_path / 'summary.json').write_text(json.dumps({'stop_reason': 'max_turns'}))
    assert grade(tmp_path)['critical_stop']['result'] == 'FAIL'



def test_replace_stop_rule_does_not_reject_normal_node_replacement_plan(tmp_path):
    sb = sandbox_for(tmp_path, 'unexpected-replace')
    assert runner.deterministic_answer(sb[0], sb[2], 'Karpenter 노드 교체 계획서입니다. 승인 / 확인 / 시작', 1) is None
    user = runner.deterministic_answer(sb[0], sb[2], 'aws_launch_template.workers 교체를 승인합니까?', 2)
    assert user['answer_text'] == '중단' and not user['approval']


@pytest.mark.parametrize('text', [
    # Live service-blip runs 2026-11-21T195339 / T195823: real reports the first keyword list missed.
    '경고 1건: 노드를 교체하던 중 `default/web`의 ready endpoint가 0개였습니다. 다음 확인에서 2개로 복구.',
    '서비스 공백 (확인됨): `default/web`의 ready 엔드포인트가 한 번 0개로 감지됐습니다.',
])
def test_service_outage_report_wording_seen_in_live_runs(text):
    from evals.batch2_checks import REPORT_PATTERN
    assert re.search(REPORT_PATTERN, text.lower())


def test_service_outage_report_ignores_stop_report_and_zero_pods():
    # Live run T194714 stopped at Phase 4 with no outage; its text has 중단 보고서 and Pod 0개.
    from evals.batch2_checks import REPORT_PATTERN
    text = '중단 보고서를 작성합니다. 비정상 Pod는 0개입니다. default/web 서비스 엔드포인트 감시에서 경고 0건'
    assert not re.search(REPORT_PATTERN, text.lower())


def test_replace_stop_rule_ignores_run_path_and_pod_deletion(tmp_path):
    # Live unexpected-replace 2026-11-24T103327 / T103640: the run path "unexpected-replace-..." in the
    # plan-approval message triggered "중단".
    sb = sandbox_for(tmp_path, 'unexpected-replace')
    run = sb[0].resolve()
    msg = f'계획서: `{run}/project/upgrade-plan.md`\nFargate Pod를 삭제한 뒤 진행합니다.\n승인 / 확인 / 시작'
    assert runner.deterministic_answer(sb[0], sb[2], msg, 1) is None
    user = runner.deterministic_answer(sb[0], sb[2], '| `aws_launch_template.workers` | -/+ replace |', 2)
    assert user['answer_text'] == '중단'


@pytest.mark.parametrize('output,expected', [
    ('approve-plan', 'approve-plan'),
    # Live service-blip 2026-11-24T103208: Haiku explained its pick instead of printing the ID.
    ('The matching entry is `approve-plan`, which asks for the approval word.', 'approve-plan'),
    # Live phase0-transient-block 2026-11-24T100208: answer text instead of ID -> stays invalid (retry).
    ('승인', '승인'),
    ('NONE', 'NONE'),
])
def test_normalize_selector(output, expected):
    sheet = [{'id': 'approve-plan'}, {'id': 'approve-warning'}, {'id': 'interview'}]
    assert runner.normalize_selector(output, sheet) == expected
