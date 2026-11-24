#!/usr/bin/env python3
"""Apply policy profiles to an existing docker-sbx sandbox.

Usage:
    python3 docker-sbx-apply-profile.py -n NAME -profile-policy NAMES [-options]
    python3 docker-sbx-apply-profile.py -n NAME -profile-directory NAMES [-options]

    python3 docker-sbx-apply-profile.py --help

Applies the named policy profiles' and/or directory bundles' network rules and
Claude assets to a sandbox that already exists, then records them in the
sandbox's saved config. A bundle that carries a kit is refused, because kits are
built at creation. It never creates a sandbox and never scans the project for
secrets.

The implementation lives in docker-sbx-create-sandbox.py, next to this file, so
creation and updates share one profile loader, one asset packer and one config
writer. This script is the command-line entry point for the update half.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_create_module():
    """Load docker-sbx-create-sandbox.py from this script's directory.

    A hyphenated filename cannot be imported by name; the same loader is what the
    test suites use. Keeping the two scripts side by side is what makes this work,
    so a missing file is reported clearly rather than as an ImportError.
    """
    script = Path(__file__).resolve().parent / "docker-sbx-create-sandbox.py"
    if not script.is_file():
        print(
            f"Error: {script} was not found - keep docker-sbx-apply-profile.py "
            "next to docker-sbx-create-sandbox.py",
            file=sys.stderr,
        )
        return None

    spec = importlib.util.spec_from_file_location("docker_sbx_create_sandbox", script)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken checkout, not a user mistake
        print(f"Error: could not load {script}: {exc}", file=sys.stderr)
        return None
    return module


def main(argv=None) -> int:
    module = load_create_module()
    if module is None:
        return 1
    return module.main_update(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
