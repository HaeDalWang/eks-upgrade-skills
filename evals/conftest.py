import json
import os
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
