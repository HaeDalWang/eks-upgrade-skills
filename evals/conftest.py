import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.runner import SCENARIO, prepare


def pytest_configure(config):
    config.addinivalue_line('markers', 'eval_live: opt-in paid Claude end-to-end evaluation')


def pytest_collection_modifyitems(items):
    if os.environ.get('K8S_EVAL') != '1':
        for item in items:
            if item.get_closest_marker('eval_live'):
                item.add_marker(pytest.mark.skip(reason='set K8S_EVAL=1 to run paid eval'))


@pytest.fixture
def sandbox(tmp_path):
    scenario = json.loads(SCENARIO.read_text())
    run = tmp_path / 'run'
    env = prepare(run, scenario, Path('/not-used/claude'))
    return run, env, scenario


@pytest.fixture(autouse=True)
def offline_plugin_cli(monkeypatch, request):
    """Model only plugin installation offline; live tests use the real prepare path."""
    if request.node.get_closest_marker('eval_live'):
        return
    from evals import runner
    original = subprocess.run

    def run(command, *args, **kwargs):
        if isinstance(command, list) and len(command) > 1 and command[1] == 'plugin':
            home = Path(kwargs['env']['HOME'])
            cache = home / '.claude/plugins/cache/eks-upgrade-skills/eks-upgrade-skills/0.1.0'
            if command[2] == 'install':
                for name in ('.claude-plugin', 'hooks', 'eks-upgrade-skills', 'helm-k8s-compat'):
                    shutil.copytree(runner.REPO / name, cache / name,
                                    ignore=shutil.ignore_patterns('__pycache__'))
            output = json.dumps([{'id': 'eks-upgrade-skills@eks-upgrade-skills',
                                  'enabled': True}]) if command[2] == 'list' else 'installed\n'
            return subprocess.CompletedProcess(command, 0, output, '')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
