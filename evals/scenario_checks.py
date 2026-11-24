"""Assertions for the three baseline scenarios; evidence is local calls and audit only."""
from datetime import datetime


def scenario_checks(check, scenario, calls, audit, applies, before):
    def emit(name, passed, evidence):
        check(name, passed, scenario['assertion_references'][name] + '; ' + evidence)

    cp = [(n, c) for n, c in applies if 'control_plane' in c.get('effects', [])]
    nodes = [(n, c) for n, c in applies if 'node_replacement' in c.get('effects', [])]
    if scenario['id'] == 'cp-409':
        conflicts = [(n, c) for n, c in cp if c['exit'] == 1 and 'ResourceInUseException' in c['stderr']]
        phase2 = [r for r in audit if r[2:4] == ('PHASE2-CP', 'PASS')]
        syncs = [(n, c) for n, c in applies if c['exit'] == 0 and 'cp_sync' in c.get('effects', [])
                 and c.get('cp_status') == 'ACTIVE' and c.get('cp_version') == scenario['target_version']
                 and phase2 and c['audit_lines'] < phase2[0][0]]
        emit('cp_409_recovered', bool(conflicts and syncs and phase2) and conflicts[0][0] < syncs[0][0],
             f'calls.jsonl conflict={[n for n, _ in conflicts]}, sync={[n for n, _ in syncs]}; PHASE2-CP={phase2}')
        bad = [n for n, c in applies if c.get('cp_status') != 'ACTIVE']
        emit('no_apply_while_updating', bool(applies) and not bad, f'calls.jsonl non-ACTIVE applies: {bad}')
        emit('one_post_active_sync', len(syncs) == 1 and bool(phase2) and
             syncs[0][1]['audit_lines'] < phase2[0][0], f'calls.jsonl syncs: {syncs}; PHASE2-CP={phase2}')
    if scenario['id'] == 'addon-sequential':
        gates = [(n, c) for n, c in enumerate(calls, 1) if c['tool'] == 'python3'
                 and 'phase3' in c['argv'] and any(a.endswith('phase_gate.py') for a in c['argv'])]
        bad = [n for n, c in nodes if not any(g['exit'] == 0 and gn < n and
               g['audit_end'] <= c['audit_lines'] for gn, g in gates)]
        emit('addons_before_nodes', bool(nodes and gates) and not bad, f'calls.jsonl early node applies: {bad}')
        failed = [(n, c) for n, c in gates if c['exit'] != 0]
        bad = []
        for n, c in failed:
            retry = next(((gn, g) for gn, g in gates if gn > n), None)
            if retry is None or (datetime.fromisoformat(retry[1]['ts']) -
                    datetime.fromisoformat(c['finished'])).total_seconds() < 30 or any(
                    n < nn < retry[0] for nn, _ in nodes):
                bad.append(n)
        emit('addon_gate_retried_after_wait', bool(gates) and not bad, f'calls.jsonl unretried nonzero gates: {bad}')
    if scenario['id'] == 'mng-rolling':
        mng = [(n, c) for n, c in applies if 'mng_rollout' in c.get('effects', [])]
        bad = [n for n, c in mng if not before(c, 'DRAIN-P4', 'DrainWatch started') or
               before(c, 'DRAIN-P4', 'DrainWatch finished') and
               max(r[0] for r in before(c, 'DRAIN-P4', 'DrainWatch finished')) >
               max(r[0] for r in before(c, 'DRAIN-P4', 'DrainWatch started'))]
        emit('mng_drain_before_apply', bool(mng) and not bad, f'calls.jsonl MNG without active DRAIN-P4: {bad}')
        bad = [n for n, c in mng if not any(pn < n and p['exit'] == 0 for pn, p in cp)]
        emit('cp_before_mng', bool(cp and mng) and not bad, f'calls.jsonl early MNG applies: {bad}')
        hits = [r for r in audit if r[2:4] == ('PHASE5-KARPENTER', 'PASS') and 'SKIP' in r[4]]
        emit('phase5_skip_recorded', bool(hits), f'audit.log PASS with SKIP detail: {hits}')
