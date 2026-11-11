"""Validate target wheel metadata without importing target native extensions."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import packaging

CHECKER = Path(__file__).resolve().parents[3] / "scripts/validate-fnos-native-vendor.py"


def distribution(root, name, version, *requirements):
    directory = root / f"{name.replace('-', '_')}-{version}.dist-info"
    directory.mkdir()
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {value}\n" for value in requirements)
    )


@pytest.fixture
def vendor(tmp_path):
    shutil.copytree(Path(packaging.__file__).parent, tmp_path / "packaging")
    distribution(tmp_path, "packaging", "26.0")
    distribution(tmp_path, "zleap-sag", "0.15.0", "lancedb>=0.16")
    distribution(tmp_path, "lancedb-compat", "0.41.0", "lance-namespace>=0.13.0", "packaging>=23")
    distribution(tmp_path, "lance-namespace", "0.13.0", "lance-namespace-urllib3-client==0.13.0")
    distribution(tmp_path, "lance-namespace-urllib3-client", "0.13.0")
    return tmp_path


def check(vendor):
    return subprocess.run([sys.executable, str(CHECKER), "--vendor", str(vendor), "--platform", "linux/amd64"],
                          capture_output=True, text=True)


def test_official_compat_satisfies_only_sdk_lancedb_requirement(vendor):
    assert check(vendor).returncode == 0


@pytest.mark.parametrize("defect", ["standard-package", "missing-package", "wrong-version", "unapproved-alias",
                                  "target-linux", "target-x86", "target-python", "sdk-version-mismatch"])
def test_missing_incompatible_or_unapproved_target_dependencies_fail(vendor, defect):
    if defect == "standard-package":
        distribution(vendor, "lancedb", "0.34.0")
    elif defect == "missing-package":
        distribution(vendor, "dependent", "1.0", "absent>=1")
    elif defect == "wrong-version":
        distribution(vendor, "dependent", "1.0", "packaging<20")
    elif defect == "unapproved-alias":
        distribution(vendor, "dependent", "1.0", "lancedb>=0.16")
    elif defect == "target-linux":
        distribution(vendor, "dependent", "1.0", "absent; sys_platform == 'linux'")
    elif defect == "target-x86":
        distribution(vendor, "dependent", "1.0", "absent; platform_machine == 'x86_64'")
    elif defect == "target-python":
        distribution(vendor, "dependent", "1.0", "absent; python_version >= '3.12'")
    elif defect == "sdk-version-mismatch":
        (vendor / "zleap_sag-0.15.0.dist-info/METADATA").write_text(
            "Name: zleap-sag\nVersion: 0.15.0\nRequires-Dist: lancedb<0.40\n"
        )
    assert check(vendor).returncode != 0


def test_host_platform_and_unselected_extras_do_not_require_target_packages(vendor):
    distribution(vendor, "dependent", "1.0", "absent; sys_platform == 'darwin'",
                 "absent; platform_machine == 'aarch64'", "absent; python_version < '3.12'",
                 "absent; extra == 'dev'")
    assert check(vendor).returncode == 0
