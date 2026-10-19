"""Contracts required by the fnOS SDK upgrade without changing extraction defaults."""

from importlib.metadata import version


def test_engine_version_is_015():
    assert version("zleap-sag") == "0.15.0"
