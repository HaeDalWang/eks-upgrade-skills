"""Milestone 3 plan order assertions over generated documents and audit records."""
import hashlib
import json
from pathlib import Path

from upgrade_plan import fields, validate

PLAN_ASSERTIONS = ('approved_plan_step_order', 'no_step_type_shopping')


def document(run, source, digest):
    # Archived runs keep generated documents even when the original cwd is gone.
    for candidate in (Path(source), run / 'project' / Path(source).name, run / Path(source).name):
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            return validate(json.loads(candidate.read_text()))
    raise ValueError('missing or edited approved plan: ' + source)


def plan_checks(check, run, audit):
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
