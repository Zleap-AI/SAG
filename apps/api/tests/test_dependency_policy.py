import tomllib
from pathlib import Path

import yaml


def test_zleap_sag_stays_on_the_supported_0_13_0_hotfix_release():
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]

    assert "zleap-sag==0.13.0+sag.1" in project["dependencies"]
    assert project["optional-dependencies"]["postgres"] == [
        "asyncpg>=0.29",
        "zleap-sag[postgres]==0.13.0+sag.1",
    ]
    assert pyproject["tool"]["uv"]["sources"]["zleap-sag"] == {
        "path": "vendor/zleap_sag-0.13.0+sag.1-py3-none-any.whl"
    }


def test_compose_healthcheck_allows_large_storage_upgrade_to_finish():
    compose = yaml.safe_load((Path(__file__).parents[3] / "compose.yaml").read_text())
    healthcheck = compose["services"]["api"]["healthcheck"]

    assert healthcheck["start_period"] == "60s"
    assert healthcheck["retries"] >= 480
