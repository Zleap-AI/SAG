import tomllib
from pathlib import Path


def test_zleap_sag_stays_on_the_supported_0_7_1_release():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    project = pyproject["project"]

    assert "zleap-sag==0.7.1" in project["dependencies"]
    assert project["optional-dependencies"]["postgres"] == [
        "asyncpg>=0.29",
        "zleap-sag[postgres]==0.7.1",
    ]
