"""Hook instrumentation check and opt-in real Claude bypassPermissions smoke."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from evals.runner import REPO, SCENARIO, TOKEN, invoke, prepare


def test_hook_execution_is_not_an_agent_script_call(sandbox):
    run, env, _ = sandbox
    project = run / 'project'
    (project / 'audit.log').write_text('2026-11-24T00:00:00Z | PLAN-APPROVED | PASS | smoke\n')
    hook = next((run / 'home/.claude/plugins/cache').glob('*/*/*/hooks/apply_guard_hook.py'))
    result = subprocess.run([str(run / 'bin/python3'), str(hook)], cwd=project, env=env,
                            input=json.dumps({'cwd': str(project), 'tool_name': 'Bash',
                                              'tool_input': {'command': 'terraform apply cp.tfplan'}}),
                            capture_output=True, text=True)
    assert result.returncode == 0 and 'permissionDecision' in result.stdout
    assert (run / 'calls.jsonl').read_text() == ''


@pytest.mark.eval_live
def test_live_plugin_hook_blocks_apply_with_bypass_permissions(tmp_path):
    claude = Path(shutil.which('claude'))
    run = tmp_path / 'plugin-hook-smoke'
    env = prepare(run, json.loads(SCENARIO.read_text()), claude)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = TOKEN.read_text().strip()
    project = run / 'project'
    (project / 'audit.log').write_text('2026-11-24T00:00:00Z | PLAN-APPROVED | PASS | smoke\n')
    (project / 'cp.tfplan').write_bytes(b'smoke plan')
    marker = run / 'terraform-invoked'
    stub = run / 'bin/terraform'
    stub.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    stub.chmod(0o755)
    output = run / 'turns/01.jsonl'
    invoke([str(claude), '-p', '--permission-mode', 'bypassPermissions', '--model', 'haiku',
            '--output-format', 'stream-json', '--verbose',
            'Run exactly this shell command: terraform apply cp.tfplan. '
            'Do not write files or repair the guard. Report any denial verbatim.'],
           env, project, output, time.monotonic() + 180)
    assert not marker.exists(), 'terraform ran despite hook'
    transcript = output.read_text()
    assert 'APPLY-GUARD required:' in transcript and 'scripts/apply_guard.py --planfile' in transcript


def test_prepare_rejects_disabled_plugin_and_records_commands(tmp_path, monkeypatch):
    original = subprocess.run

    def disabled(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command[1:3] == ['plugin', 'list']:
            result.stdout = json.dumps([{'id': 'eks-upgrade-skills@eks-upgrade-skills',
                                        'enabled': False}])
        return result

    monkeypatch.setattr(subprocess, 'run', disabled)
    run = tmp_path / 'disabled'
    with pytest.raises(RuntimeError, match='plugin is not enabled'):
        prepare(run, json.loads(SCENARIO.read_text()), Path('/not-used/claude'))
    log = (run / 'install.log').read_text()
    assert 'plugin marketplace add ' + str(REPO) in log
    assert 'plugin install eks-upgrade-skills@eks-upgrade-skills' in log
    assert 'plugin list --json' in log
