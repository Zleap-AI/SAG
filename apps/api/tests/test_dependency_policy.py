import tomllib
from pathlib import Path

import yaml


def test_zleap_sag_stays_on_the_supported_0_12_0_release():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    project = pyproject["project"]

    assert "zleap-sag==0.12.0" in project["dependencies"]
    assert project["optional-dependencies"]["postgres"] == [
        "asyncpg>=0.29",
        "zleap-sag[postgres]==0.12.0",
    ]


def test_compose_healthcheck_allows_large_storage_upgrade_to_finish():
    compose = yaml.safe_load((Path(__file__).parents[3] / "compose.yaml").read_text())
    healthcheck = compose["services"]["api"]["healthcheck"]

    assert healthcheck["start_period"] == "60s"
    assert healthcheck["retries"] >= 480
