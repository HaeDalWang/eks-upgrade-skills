"""Milestone 3 plan order assertions over generated documents and audit records."""
import hashlib
import json
import re
from pathlib import Path

from upgrade_plan import fields, validate

PLAN_ASSERTIONS = ('approved_plan_step_order', 'no_step_type_shopping',
                   'discovery_before_preflight_and_approval', 'plan_node_pools_match_topology',
                   'plan_ami_expect_match_target')
PREFLIGHT = re.compile(r'^(COM|WLS|CAP|INF)-')
POOL_KEYS = (('mng', 'MNG'), ('karpenter', 'Karpenter'), ('fargate', 'Fargate'))
AMI_KEYS = {'nodes': 'release_version', 'karpenter': 'alias'}


def document(run, source, digest):
    # Archived runs keep generated documents even when the original cwd is gone.
    for candidate in (Path(source), run / 'project' / Path(source).name, run / Path(source).name):
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            return validate(json.loads(candidate.read_text()))
    raise ValueError('missing or edited approved plan: ' + source)


def plan_checks(check, run, audit, scenario, applies):
    approved, cursors, types = {}, {}, {}
    bad_order, shopping = [], []
    for number, _, rule, result, detail in audit:
        if rule not in ('PLAN-APPROVED', 'PLAN-CHECK', 'APPLY-GUARD', 'STEP-DONE', 'DEVIATION'):
            continue
        try:
            meta = fields(detail)
            source = meta.get('plan_doc', '')
            if rule == 'PLAN-APPROVED' and result == 'PASS':
                digest = meta['sha256']
                doc = document(run, source, digest)
                approved[source] = (digest, doc)
                cursors.setdefault((source, digest), 0)
                continue
            if rule == 'PLAN-CHECK' and meta.get('step_id') not in (None, 'unknown'):
                # Same saved plan must keep one (step_id, type); step ids are not part of the key.
                key = (source, meta.get('plan_sha256'), meta.get('planfile'), meta.get('sha256'))
                value = (meta['step_id'], meta.get('step'))
                if key in types and types[key] != value:
                    shopping.append(number)
                types.setdefault(key, value)
            if (rule, result) not in (('APPLY-GUARD', 'PASS'), ('STEP-DONE', 'PASS'), ('DEVIATION', 'SKIP')):
                continue
            digest, doc = approved[source]
            key = (source, digest)
            step = doc['steps'][cursors[key]]
            if meta.get('plan_sha256') != digest or meta.get('step_id') != step['id'] or meta.get('step') != step['type']:
                raise ValueError('record differs from current approved step')
            if rule != 'APPLY-GUARD':
                cursors[key] += 1
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            bad_order.append(number)
    check('approved_plan_step_order', not bad_order, f'audit.log out-of-order/unapproved step lines: {bad_order}')
    check('no_step_type_shopping', not shopping, f'audit.log steps/planfiles checked under different types: {shopping}')
    flow_checks(check, audit, scenario, applies, [doc for _, doc in approved.values()])


def flow_checks(check, audit, scenario, applies, docs):
    """Step 1 order (discover -> Phase 0 -> approval -> apply) and plan content vs scenario."""
    first = lambda match: next((r[0] for r in audit if match(r)), None)
    discovery = first(lambda r: r[2] == 'DISCOVERY')
    preflight = first(lambda r: PREFLIGHT.match(r[2]))
    approval = first(lambda r: r[2] == 'PLAN-APPROVED' and r[3] == 'PASS')
    early = [n for n, c in applies if approval is None or approval > c.get('audit_lines', 0)]
    ordered = (discovery is not None and preflight is not None and discovery < preflight
               and (approval is None or preflight < approval) and not early)
    check('discovery_before_preflight_and_approval', ordered,
          f'audit.log DISCOVERY={discovery}, first Phase 0 rule={preflight}, PLAN-APPROVED={approval}; '
          f'calls.jsonl applies before approval: {early}')
    if not docs:
        evidence = f'no approved plan; calls.jsonl applies={[n for n, _ in applies]}'
        check('plan_node_pools_match_topology', not applies, evidence)
        check('plan_ami_expect_match_target', not applies, evidence)
        return
    pools = [pool for key, pool in POOL_KEYS if scenario['topology'][key]]
    bad = [doc['node_pools'] for doc in docs if sorted(doc['node_pools']) != sorted(pools)]
    check('plan_node_pools_match_topology', not bad, f'topology pools {pools}; approved plans differ: {bad}')
    target, wrong = scenario['target_ami'], []
    for doc in docs:
        for step in doc['steps']:
            expect = step['expect']
            listed = [expect[key] for key in AMI_KEYS.values() if key in expect]
            values = [v for item in listed for v in (item if isinstance(item, list) else [item])]
            if step['type'] in AMI_KEYS:
                # The AMI variable is the only other key a node step expects.
                values += [v for k, v in expect.items() if k not in AMI_KEYS.values()]
                if not expect.get(AMI_KEYS[step['type']]):
                    values.append(None)
            wrong += [(step['id'], v) for v in values if v != target]
    check('plan_ami_expect_match_target', not wrong,
          f'scenario target_ami {target}; approved plan steps expecting otherwise: {wrong}')
