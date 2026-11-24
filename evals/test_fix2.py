"""Offline regressions for batch-1 live harness failures."""
import json
import time
from pathlib import Path

import pytest

from evals import runner
from evals.grading import grade, is_infra_error, read_jsonl
from evals.test_harness import cli, update_tfvars
from evals.test_scenarios import finish_cp, sandbox_for


@pytest.mark.parametrize('text', [
    'Request timed out. If the API is slow to answer, set API_TIMEOUT_MS.',
    "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)",
    'API Error: 500 Internal Server Error', 'API Error: 503 Service Unavailable',
    'API Error: 5xx', 'API Error: 529 overloaded', 'overloaded',
])
@pytest.mark.parametrize('exit_code', [0, 1])
def test_api_failure_is_infra_error_and_excluded_from_grade(sandbox, monkeypatch, text, exit_code):
    run, env, scenario = sandbox

    def invoke(cmd, env, project, output, deadline):
        output.write_text(json.dumps({'type': 'result', 'result': text, 'is_error': True}))
        return exit_code

    monkeypatch.setattr(runner, 'invoke', invoke)
    assert runner.converse(run, scenario, Path('/fake/claude'), 'test', env,
                           time.monotonic() + 10) == (1, 'infra_error')
    # No summary needed: historical evidence alone is sufficient for regrading.
    result = grade(run)
    assert result['stop_reason'] == 'infra_error'
    assert result['excluded_from_pass_rate'] is True
    assert 'turns/01.jsonl' in result['evidence']
    assert json.loads((run / 'grade.json').read_text()) == result


def test_report_mentioning_api_error_is_not_excluded():
    assert not is_infra_error('The tool reported API Error: 503; I stopped safely.')
    assert not is_infra_error('API Error: 401 Unauthorized')


def test_live_pytest_skips_infra_result(tmp_path, monkeypatch):
    from evals import test_harness
    scenario = runner.SCENARIO

    def live(run, *args, **kwargs):
        run.mkdir()
        (run / 'grade.json').write_text(json.dumps({'excluded_from_pass_rate': True}))

    monkeypatch.setattr(test_harness, 'REPO', tmp_path)
    (tmp_path / 'evals').mkdir()
    monkeypatch.setattr(test_harness, 'run_live', live)
    with pytest.raises(pytest.skip.Exception, match='infra_error'):
        test_harness.test_live_scenario(scenario)


@pytest.mark.parametrize('selection', ['approve-plan', 'NONE', 'approve-plan\nextra'])
def test_selector_long_recorded_approval_uses_tail_and_retries_none(sandbox, monkeypatch, selection):
    run, env, scenario = sandbox
    recorded = (Path(__file__).parent / 'fixtures/approval-message.txt').read_text()
    message = 'EARLY_CONTEXT_MUST_BE_TRIMMED\n' + 'context\n' * 500 + recorded
    replies = iter([message, 'NONE', selection, 'EVAL_DONE'])
    commands = []

    def invoke(cmd, env, project, output, deadline):
        commands.append(cmd)
        output.write_text(json.dumps({'type': 'result', 'session_id': 'test',
                                      'result': next(replies)}))
        return 0

    monkeypatch.setattr(runner, 'invoke', invoke)
    assert runner.converse(run, scenario, Path('/fake/claude'), 'test', env,
                           time.monotonic() + 10) == (2, 'done')
    for cmd in commands[1:3]:
        assert 'EARLY_CONTEXT_MUST_BE_TRIMMED' not in cmd[2]
        assert message[-2500:] in cmd[2]
        assert '**승인** / **확인** / **시작**' in cmd[2]
    users = read_jsonl(run / 'user.jsonl')
    assert [u['attempt'] for u in users] == [1, 2]
    assert users[0]['status'] == 'RETRY' and not users[0]['approval']
    expected = '승인' if selection == 'approve-plan' else runner.INVALID
    assert commands[3][2] == expected
    assert users[1]['answer_text'] == expected
    assert len(list((run / 'fake-user').glob('*.jsonl'))) == 2


def test_exact_kubectl_ec2nodeclass_range_jsonpath(sandbox):
    result = cli(sandbox, 'kubectl', 'get', 'ec2nodeclass', '-o',
                 'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.amiSelectorTerms}{"\\n"}{end}')
    name, terms = result.stdout.strip().split(' ', 1)
    assert name == 'default'
    assert json.loads(terms) == [{'alias': sandbox[2]['initial_state']['ami_alias']}]


def test_exact_kubectl_nodes_range_jsonpath(sandbox):
    result = cli(sandbox, 'kubectl', 'get', 'nodes', '-o',
                 'jsonpath={range .items[*]}{.metadata.name} {.status.nodeInfo.architecture} {.status.nodeInfo.osImage}{"\\n"}{end}')
    assert result.stdout.splitlines()[:2] == ['worker-a amd64 Bottlerocket', 'worker-b amd64 Bottlerocket']


@pytest.mark.parametrize('expression', [
    '{range .items[*]}{.metadata.name[0]}{end}',
    '{range .items[*]}{range .spec[*]}{end}{end}', '{.items[?(@.name)]}',
])
def test_jsonpath_unsupported_constructs_fail_even_for_empty_lists(sandbox, expression):
    result = cli(sandbox, 'kubectl', 'get', 'events', '-o', 'jsonpath=' + expression, check=False)
    assert result.returncode == 1 and 'shim: unsupported' in result.stderr


def nodegroup(sb):
    return json.loads(cli(sb, 'aws', 'eks', 'describe-nodegroup', '--cluster-name', 'demo-eks',
        '--nodegroup-name', 'demo-system', '--query',
        '{status:nodegroup.status, version:nodegroup.version, rel:nodegroup.releaseVersion}',
        '--output', 'json').stdout)


def updates(sb):
    return json.loads(cli(sb, 'aws', 'eks', 'list-updates', '--name', 'demo-eks',
                         '--nodegroup-name', 'demo-system', '--output', 'json').stdout)


def test_exact_aws_describe_nodegroup_query_release_version(tmp_path):
    sb = sandbox_for(tmp_path, 'mng-rolling')
    initial = sb[2]['initial_state']['ami_alias']
    assert nodegroup(sb) == {'status': 'ACTIVE', 'version': '1.33', 'rel': initial}
    finish_cp(sb)
    update_tfvars(sb[0], initial, '1.34.0-20261121')
    cli(sb, 'terraform', 'apply', '-target=module.eks.module.eks_managed_node_group')
    for _ in range(sb[2]['transitions']['node_polls']):
        assert nodegroup(sb) == {'status': 'UPDATING', 'version': '1.33', 'rel': initial}
    assert nodegroup(sb) == {'status': 'ACTIVE', 'version': '1.34', 'rel': '1.34.0-20261121'}


def test_exact_aws_list_updates_and_describe_update_follow_mng_rollout(tmp_path):
    sb = sandbox_for(tmp_path, 'mng-rolling')
    assert updates(sb) == {'updateIds': []}
    finish_cp(sb)
    update_tfvars(sb[0], sb[2]['initial_state']['ami_alias'], '1.34.0-20261121')
    cli(sb, 'terraform', 'apply', '-target=module.eks.module.eks_managed_node_group')
    for expected in ['InProgress'] * sb[2]['transitions']['node_polls'] + ['Successful']:
        assert updates(sb) == {'updateIds': ['eval-update']}
        result = cli(sb, 'aws', 'eks', 'describe-update', '--name', 'demo-eks',
                     '--nodegroup-name', 'demo-system', '--update-id', 'eval-update', '--output', 'json')
        assert json.loads(result.stdout)['update']['status'] == expected
    assert nodegroup(sb)['version'] == '1.34'
    assert updates(sb) == {'updateIds': ['eval-update']}


def test_describe_nodegroup_health_query_seen_in_live_run(sandbox):
    # Live run mng-rolling 2026-11-21T164405, calls.jsonl line 143.
    result = cli(sandbox, 'aws', 'eks', 'describe-nodegroup', '--cluster-name', 'demo-eks',
        '--nodegroup-name', 'demo-system', '--query',
        '{status:nodegroup.status, version:nodegroup.version, release:nodegroup.releaseVersion, health:nodegroup.health}',
        '--output', 'json')
    assert json.loads(result.stdout)['health'] == {'issues': []}


@pytest.mark.parametrize('argv', [
    # Batch-2 live runs 2026-11-21 (unexpected-replace, ok-approval, phase0-transient-block, service-blip).
    ['terraform', 'state', 'list'],
    ['aws', 'eks', 'describe-addon', '--cluster-name', 'demo-eks', '--addon-name', 'vpc-cni',
     '--query', '[addon.addonName,addon.addonVersion,addon.status]', '--output', 'text'],
    ['kubectl', 'get', 'deploy,pods', '-n', 'default', '-o', 'wide'],
    ['kubectl', 'get', 'nodes', '-o', 'jsonpath={.items[*].status.nodeInfo.kubeletVersion}'],
])
def test_batch2_live_argv_supported(sandbox, argv):
    result = cli(sandbox, *argv)
    assert result.returncode == 0 and result.stdout.strip()


def test_describe_addon_list_query_text(sandbox):
    out = cli(sandbox, 'aws', 'eks', 'describe-addon', '--cluster-name', 'demo-eks', '--addon-name',
              'vpc-cni', '--query', '[addon.addonName,addon.addonVersion,addon.status]', '--output', 'text').stdout
    assert out.split('\t')[0] == 'vpc-cni'


def test_eval_done_with_unanswered_rule_question_gets_answer(sandbox, monkeypatch):
    # Live unexpected-replace run: the agent asked about the replace and printed EVAL_DONE in the same turn.
    run, env, scenario = sandbox
    scenario = {**scenario, 'answers': [{'id': 'stop-replace', 'answer_text': '중단'}],
                'answer_rules': [{'id': 'replace', 'pattern': 'PLAN-CHECK WARN', 'answer_ids': ['stop-replace']}]}
    replies = iter(['PLAN-CHECK WARN — 어떻게 할까요?\nEVAL_DONE', '중단했습니다. PLAN-CHECK WARN\nEVAL_DONE'])
    commands = []

    def invoke(cmd, env, project, output, deadline):
        commands.append(cmd)
        output.write_text(json.dumps({'type': 'result', 'session_id': 'test', 'result': next(replies)}))
        return 0

    monkeypatch.setattr(runner, 'invoke', invoke)
    assert runner.converse(run, scenario, Path('/fake/claude'), 'test', env,
                           time.monotonic() + 10) == (2, 'done')
    assert commands[1][2] == '중단'
    assert [u['rule'] for u in read_jsonl(run / 'user.jsonl')] == ['replace']


def test_drain_check_pairs_monitors_by_pid():
    # Live run: DRAIN-P5 started (110), then DRAIN-P4 finished (114), then the Karpenter apply (117).
    from evals.grading import drain_check
    audit = [(110, '', 'DRAIN-P5', 'START', 'pid=2 DrainWatch started: scope=all'),
             (114, '', 'DRAIN-P4', 'STOP', 'pid=1 DrainWatch finished: events=0')]
    before = lambda call, rule, text='': [r for r in audit if r[0] <= call['audit_lines']
                                          and r[2] == rule and text in r[4]]
    apply = {'audit_lines': 117, 'effects': ['node_replacement']}
    assert drain_check([(105, apply)], before) == []
    audit.append((115, '', 'DRAIN-P5', 'STOP', 'pid=2 DrainWatch finished: events=0'))
    assert drain_check([(105, apply)], before) == [105]
