"""Upgrade passing snapshots to generated-plan evidence and detect order regressions."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evals.grading import grade, read_jsonl, records
from evals.plan_checks import PLAN_ASSERTIONS, plan_checks
from evals.test_batch2 import fixture as batch2_fixture, NAMES
from evals.test_scenarios import handwritten_fixture
from upgrade_plan import default_order, detail_fields


def promote(run, name):
    if name == 'normal-karpenter-fargate':
        shutil.copytree(Path(__file__).parent / 'fixtures/passing', run, dirs_exist_ok=True)
        (run / 'audit.txt').rename(run / 'audit.log')
        scenario = json.loads((Path(__file__).parent / 'scenarios' / (name + '.json')).read_text())
    elif name in ('cp-409', 'addon-sequential', 'mng-rolling'):
        handwritten_fixture(run, name)
        scenario = json.loads((run / 'scenario.json').read_text())
    else:
        batch2_fixture(run, name)
        scenario = json.loads((run / 'scenario.json').read_text())
    scenario['safety_checks_version'] = 3
    (run / 'scenario.json').write_text(json.dumps(scenario))
    pools = [p for k, p in [('mng', 'MNG'), ('karpenter', 'Karpenter'), ('fargate', 'Fargate')]
             if scenario['topology'][k]]
    order = default_order(pools)
    doc = dict(schema_version=1, cluster='demo-eks', current_version='1.33', target_version='1.34',
               tf_dir='.', output_language='ko', node_pools=pools, deviations=[],
               steps=[dict(id=s, type=s, targets=[], expect={}) for s in order])
    path = run / 'upgrade-plan.json'
    path.write_text(json.dumps(doc))
    binding = dict(plan_doc=str(path), plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    upgrade_evidence(run, order, binding)


def upgrade_evidence(run, order, binding):
    lines = (run / 'audit.log').read_text().splitlines()
    calls = read_jsonl(run / 'calls.jsonl')
    insertions = {0: [row('PLAN-APPROVED', dict(plan_doc=binding['plan_doc'], sha256=binding['plan_sha256'], answer='승인'))]}
    previous = None
    for call in calls:
        if call['tool'] != 'terraform' or 'apply' not in call['argv']:
            continue
        kind = 'cp' if set(call.get('effects', [])) & {'control_plane', 'cp_sync'} else order[1]
        at = call['audit_lines']
        additions = insertions.setdefault(at, [])
        if previous and kind != previous:
            additions.append(row('STEP-DONE', dict(**binding, step_id=previous, step=previous)))
        meta = dict(**binding, step_id=kind, step=kind, planfile=kind + '.tfplan', sha256='saved', node_changes=str(kind != 'cp').lower())
        additions.extend([row('PLAN-CHECK', meta), row('APPLY-GUARD', meta)])
        call['argv'] = ['apply', kind + '.tfplan']
        previous = kind
    upgraded = []
    for offset in range(len(lines) + 1):
        upgraded.extend(insertions.get(offset, []))
        for call in calls:
            for key in ('audit_lines', 'audit_end'):
                if call.get(key) == offset:
                    call[key] = ('offset', len(upgraded))
        if offset < len(lines):
            upgraded.append(lines[offset])
    for call in calls:
        for key in ('audit_lines', 'audit_end'):
            if isinstance(call.get(key), tuple):
                call[key] = call[key][1]
    (run / 'audit.log').write_text('\n'.join(upgraded) + '\n')
    (run / 'calls.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in calls))


def row(rule, meta):
    return '2026-01-01T00:00:00Z | {} | - | PASS | {}'.format(rule, detail_fields(**meta))


@pytest.mark.parametrize('name', ['normal-karpenter-fargate', 'cp-409', 'addon-sequential', 'mng-rolling', *NAMES])
def test_passing_fixtures_grade_with_generated_plan_order(tmp_path, name):
    promote(tmp_path, name)
    grades = grade(tmp_path)
    assert all(grades[key]['result'] == 'PASS' for key in PLAN_ASSERTIONS)
    assert all(g['result'] == 'PASS' for g in grades.values()), grades


@pytest.mark.parametrize('mutation,key', [('order', 'approved_plan_step_order'), ('shopping', 'no_step_type_shopping')])
def test_plan_assertions_detect_order_and_step_type_shopping(tmp_path, mutation, key):
    promote(tmp_path, 'cp-409')
    lines = (tmp_path / 'audit.log').read_text().splitlines()
    if mutation == 'order':
        index = next(i for i, line in enumerate(lines) if 'APPLY-GUARD' in line)
        lines[index] = lines[index].replace('step_id=cp', 'step_id=full').replace('step=cp', 'step=full')
    else:
        checked = next(line for line in lines if 'PLAN-CHECK' in line)
        # Same saved planfile re-checked as a different step id and type.
        lines.append(checked.replace('step=cp', 'step=karpenter').replace('step_id=cp', 'step_id=nodes'))
    result = {}
    plan_checks(lambda name, passed, evidence: result.update({name: passed}), tmp_path, records(lines))
    assert not result[key]
