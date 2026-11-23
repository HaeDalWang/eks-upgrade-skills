"""Forward Python unchanged; record actual phase gate exits for retry assertions."""
import fcntl
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

run = Path(sys.argv[0]).absolute().parent.parent
args = sys.argv[1:]
started = datetime.now(timezone.utc).isoformat()
audit = run / 'project/audit.log'
audit_lines = len(audit.read_text().splitlines()) if audit.exists() else 0
if args and Path(args[0]).name == 'gate_check.py':
    with (run / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        scenario = json.loads((run / 'scenario.json').read_text())
        if scenario['transitions'].get('preflight_blocks'):
            state = json.loads((run / 'state.json').read_text())
            count = state.get('preflight_runs', 0)
            state['pdb_remaining'] = int(count < scenario['transitions']['preflight_blocks'])
            state['preflight_runs'] = count + 1
            (run / 'state.json').write_text(json.dumps(state))
tracked = bool(args and Path(args[0]).name in (
    'phase_gate.py', 'gate_check.py', 'plan_check.py', 'apply_guard.py', 'upgrade_plan.py'))
result = subprocess.run([sys.executable, *args], check=False,
                        capture_output=tracked, text=tracked)
if tracked:
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
if tracked:
    with (run / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (run / 'calls.jsonl').open('a') as log:
            log.write(json.dumps({'tool': 'python3', 'argv': args, 'ts': started,
                'finished': datetime.now(timezone.utc).isoformat(), 'exit': result.returncode,
                'stdout': result.stdout, 'stderr': result.stderr, 'effects': [], 'audit_lines': audit_lines,
                'audit_end': len(audit.read_text().splitlines()) if audit.exists() else 0}) + '\n')
sys.exit(result.returncode)
