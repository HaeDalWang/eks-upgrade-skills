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
result = subprocess.run([sys.executable, *args], check=False)
if args and Path(args[0]).name == 'phase_gate.py':
    with (run / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (run / 'calls.jsonl').open('a') as log:
            log.write(json.dumps({'tool': 'python3', 'argv': args, 'ts': started,
                'finished': datetime.now(timezone.utc).isoformat(), 'exit': result.returncode,
                'stderr': '', 'effects': [], 'audit_lines': audit_lines,
                'audit_end': len(audit.read_text().splitlines()) if audit.exists() else 0}) + '\n')
sys.exit(result.returncode)
