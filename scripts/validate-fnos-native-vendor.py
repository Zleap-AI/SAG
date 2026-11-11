"""Check native vendor dependency closure for the target, without loading its .so files."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path


def validate(directory: Path, platform: str) -> None:
    # packaging is a pure-Python dependency already present in the target. Do
    # not require build-host packages or import any cross-compiled native code.
    sys.path.insert(0, str(directory))
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name

    installed = {}
    for distribution in importlib.metadata.distributions(path=[str(directory)]):
        name = canonicalize_name(distribution.metadata["Name"])
        if name in installed:
            raise ValueError(f"duplicate vendor distribution: {name}")
        installed[name] = distribution
    if platform == "linux/amd64":
        if "lancedb" in installed:
            raise ValueError("x86 vendor must contain only lancedb-compat, not the AVX2 lancedb distribution")
        for name, version in {
            "lancedb-compat": "0.41.0",
            "lance-namespace": "0.13.0",
            "lance-namespace-urllib3-client": "0.13.0",
        }.items():
            if name not in installed or installed[name].version != version:
                raise ValueError(f"x86 vendor requires {name}=={version}")
        if "zleap-sag" not in installed:
            raise ValueError("x86 vendor requires zleap-sag metadata")
    environment = {
        "implementation_name": "cpython", "implementation_version": "3.12.0", "os_name": "posix",
        "platform_machine": "x86_64" if platform == "linux/amd64" else "aarch64",
        "platform_python_implementation": "CPython", "platform_release": "", "platform_system": "Linux",
        "platform_version": "", "python_full_version": "3.12.0", "python_version": "3.12",
        "sys_platform": "linux", "extra": "",
    }
    for name, distribution in sorted(installed.items()):
        requires_python = distribution.metadata.get("Requires-Python")
        if requires_python and not SpecifierSet(requires_python).contains(environment["python_full_version"]):
            raise ValueError(f"{name} requires Python {requires_python}, target is Python 3.12")
        for value in distribution.requires or []:
            requirement = Requirement(value)
            if requirement.marker is not None and not requirement.marker.evaluate(environment):
                continue
            required = canonicalize_name(requirement.name)
            # Only this SDK dependency may be fulfilled by the official
            # compat distribution sharing the lancedb import namespace.
            if platform == "linux/amd64" and name == "zleap-sag" and required == "lancedb":
                required = "lancedb-compat"
            dependency = installed.get(required)
            if dependency is None or not requirement.specifier.contains(dependency.version, prereleases=True):
                raise ValueError(f"{name} has unsatisfied target dependency: {requirement}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("linux/amd64", "linux/arm64"))
    args = parser.parse_args()
    validate(args.vendor.resolve(strict=True), args.platform)
    print("fnOS target vendor dependency closure verified")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"fnos-native-vendor: {error}")
