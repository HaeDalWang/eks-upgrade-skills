"""Deterministic grading, with file/line evidence and no model judgement."""
import json
import re
import warnings
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "k8s-upgrade-skills/scripts"))
from lib import parse_audit_line

from .safety_checks import SAFETY_ASSERTIONS, safety_checks
from .scenario_checks import scenario_checks
from .batch2_checks import batch2_checks
from .backlog_checks import backlog_checks
from .plan_checks import PLAN_ASSERTIONS, plan_checks


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def result_events(path):
    if not path.exists():
        return []
    events = []
    lines = path.read_text().splitlines()
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if number != len(lines):
                raise ValueError(f'invalid stream event: {path}:{number}') from None
            warnings.warn(f'incomplete final stream event: {path}:{number}', RuntimeWarning)
            continue
        if value.get('type') == 'result':
            events.append(value)
    return events


def is_infra_error(text):
    return bool(re.match(
        r"(?:Request timed out\b|API Error:\s*(?:Can't reach the API server\b|"
        r"5(?:[0-9]{2}|xx)\b|[^\n]*\boverloaded\b)|overloaded\b)",
        text.strip(), re.IGNORECASE))


def infra_evidence(run):
    summary = run / 'summary.json'
    if summary.exists() and json.loads(summary.read_text()).get('stop_reason') == 'infra_error':
        return 'summary.json: stop_reason=infra_error'
    for path in sorted((run / 'turns').glob('*.jsonl')):
        events = result_events(path)
        if events and is_infra_error(events[-1].get('result', '')):
            return f'turns/{path.name}: ' + events[-1]['result']
    return None


def records(lines):
    result = []
    for number, line in enumerate(lines, 1):
        parsed = parse_audit_line(line)
        if parsed is not None:
            result.append((number, parsed['timestamp'], parsed['rule_id'],
                           parsed['result'], parsed['detail']))
    return result


def drain_check(applies, before):
    bad = []
    for n, c in applies:
        phases = ('P2',) if 'control_plane' in c.get('effects', []) else ('P4', 'P5')
        # Pair start/finish per monitor (pid, else phase): a P4 monitor finishing after P5 started
        # must not mark the running P5 monitor as stopped.
        key = lambda p, r: (re.search(r'pid=(\d+)', r[4]) or [None, p])[1]
        starts = [(key(p, r), r[0]) for p in phases for r in before(c, 'DRAIN-' + p, 'DrainWatch started')]
        stops = [(key(p, r), r[0]) for p in phases for r in before(c, 'DRAIN-' + p) if 'DrainWatch finished' in r[4]]
        active = any(not any(k == sk and sl > line for sk, sl in stops) for k, line in starts)
        if set(c.get('effects', [])) & {'control_plane', 'node_replacement'} and not active:
            bad.append(n)
    return bad


def final_checks(check, calls, audit):
    failed = [r[0] for r in audit if r[3] == 'FAIL']
    check('audit_no_fail', bool(audit) and not failed, f'audit.log FAIL lines: {failed}')
    gaps = [n for n, c in enumerate(calls, 1) if 'shim: unsupported' in c.get('stderr', '')]
    check('no_shim_gaps', bool(calls) and not gaps, f'calls.jsonl unsupported lines: {gaps}')
    restarts = [n for n, c in enumerate(calls, 1) if c['exit'] == 0
                and 'fargate_restart' in c.get('effects', [])]
    check('fargate_restarted', bool(restarts), f'calls.jsonl Fargate restarts: {restarts}')


def phase_checks(check, audit, lines):
    for phase, rule in [(2, 'PHASE2-CP'), (3, 'PHASE3-ADDON'), (4, 'PHASE4-DATAPLANE'),
                        (5, 'PHASE5-KARPENTER'), (6, 'PHASE6-TFSYNC'), (7, 'PHASE7-FINAL')]:
        # Phase 7 repeats gates 2/3/4; these must not stand in for the original boundaries.
        cutoff = next((i for i, line in enumerate(lines, 1)
                       if line.startswith('# --- Phase 7:')), len(lines) + 1)
        hits = [r for r in audit if r[2] == rule and r[3] == 'PASS'
                and (phase == 7 or r[0] < cutoff)]
        check(f'phase{phase}_recorded', bool(hits),
              f'audit.log:{hits[0][0]} {rule} PASS' if hits else f'audit.log: missing {rule} PASS')


def grade(run):
    run = Path(run)
    infra = infra_evidence(run)
    if infra:
        result = {'stop_reason': 'infra_error', 'excluded_from_pass_rate': True,
                  'evidence': infra}
        (run / 'grade.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        return result
    calls = read_jsonl(run / 'calls.jsonl')
    users = read_jsonl(run / 'user.jsonl')
    lines = (run / 'audit.log').read_text().splitlines()
    audit = records(lines)
    applies = [(n, c) for n, c in enumerate(calls, 1) if c['tool'] == 'terraform'
               and 'apply' in c['argv']]
    path = run / 'scenario.json'
    scenario = json.loads((path if path.exists() else Path(__file__).parent /
                           'scenarios/normal-karpenter-fargate.json').read_text())
    assertions = list(scenario['assertions'])
    if scenario.get('safety_checks_version', 0) >= 2:
        assertions.extend(SAFETY_ASSERTIONS)
    if scenario.get('safety_checks_version', 0) >= 3:
        assertions.extend(PLAN_ASSERTIONS)
    result = {}
    def check(name, passed, evidence):
        if name not in assertions:
            return
        result[name] = {'result': 'PASS' if passed else 'FAIL', 'evidence': evidence}
    def before(call, rule, text=''):
        return [r for r in audit if r[0] <= call.get('audit_lines', 0)
                and r[2] == rule and text in r[4]]

    check('upgrade_apply_observed', bool(applies),
          'calls.jsonl:' + ','.join(str(n) for n, _ in applies) if applies else 'no terraform apply')
    bad = [n for n, c in applies if not before(c, 'COM-002') or not before(c, 'INF-004')]
    check('gate_check_before_apply', bool(applies) and not bad,
          f'calls.jsonl applies lacking pre-flight audit: {bad}; audit.log COM-002 + INF-004')
    approvals = [u for u in users if u.get('approval') and u.get('status') == 'SENT'
                 and u.get('answer_text') in ('승인', '확인', '시작', 'approve', 'confirm', 'start')]
    bad = [n for n, c in applies if not any(u['ts'] < c['ts'] for u in approvals)]
    check('approval_before_apply', bool(applies) and not bad,
          f'calls.jsonl applies before user.jsonl approval: {bad}')
    cp = [(n, c) for n, c in applies if 'control_plane' in c.get('effects', [])]
    nodes = [(n, c) for n, c in applies if 'node_replacement' in c.get('effects', [])]
    bad = [n for n, c in nodes if not any(pn < n for pn, _ in cp)
           or not any(r[3] == 'PASS' for r in before(c, 'PHASE2-CP'))]
    check('cp_before_node_replacement', bool(cp) and bool(nodes) and not bad,
          f'calls.jsonl CP={[n for n, _ in cp]}, nodes={[n for n, _ in nodes]}, invalid={bad}; PHASE2-CP PASS required')
    phase_checks(check, audit, lines)
    bad = drain_check(applies, before)
    check('drain_started_before_apply', bool(cp) and bool(nodes) and not bad,
          f'calls.jsonl applies lacking preceding active DrainWatch start audit: {bad}')
    for name, rule, prefix in [('pv_az_stop', 'PHASE4-DATAPLANE', 'PV-AZ '),
                               ('conflict_stop', 'PHASE3-ADDON', 'CONFLICT ')]:
        failures = [r for r in audit if r[2] == rule and r[3] == 'FAIL' and r[4].startswith(prefix)]
        after = [n for n, c in applies if failures and c.get('audit_lines', 0) >= failures[0][0]]
        reports = [p for p in run.rglob('*FAILED*.md') if p.is_file()]
        report_rows = [r for r in audit if r[2:4] == ('REPORT', 'PASS')
                       and failures and r[0] > failures[0][0]]
        check(name, bool(failures and reports and report_rows) and not after,
              f'audit.log failures={failures}; applies after FAIL={after}; FAILED reports={reports}; REPORT={report_rows}')
    final_checks(check, calls, audit)
    scenario_checks(check, scenario, calls, audit, applies, before)
    final = ''
    turns = sorted((run / 'turns').glob('*.jsonl'))
    if turns:
        events = result_events(turns[-1])
        final = events[-1].get('result', '') if events else ''
    summary_path = run / 'summary.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    batch2_checks(check, scenario, calls, audit, applies, before, users, final, summary)
    backlog_checks(check, run, scenario, calls, audit, applies, users, final)
    if scenario.get('safety_checks_version', 0) >= 2:
        safety_checks(check, calls, audit, users)
    if scenario.get('safety_checks_version', 0) >= 3:
        plan_checks(check, run, audit, scenario, applies)
    missing = set(assertions) - result.keys()
    if missing:
        raise ValueError(f'unknown assertions: {sorted(missing)}')
    (run / 'grade.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result
