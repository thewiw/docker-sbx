#!/usr/bin/env python3
"""Unit tests for docker-sbx-install.py (standard library only).

The installer mutates the host, so only the pure helpers are covered here:
prompt parsing, platform/locale selection, profile-path resolution and the
idempotency of the environment block. Everything that shells out is exercised
by `--dry-run` instead.

    python3 -m unittest discover -s test -p 'test_*.py'
    (or, from inside test/:  python3 -m unittest test_docker_sbx_install)
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The module name has hyphens, so it cannot be imported by name. The installer
# sits in the repository root, one level up from this test/ directory.
_SPEC = importlib.util.spec_from_file_location(
    "docker_sbx_install",
    Path(__file__).resolve().parent.parent / "docker-sbx-install.py",
)
install = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install)


class ConfirmTests(unittest.TestCase):
    """confirm() must match the bash helper: yes, no, EOF - never exit."""

    def setUp(self):
        install.DRY_RUN = False

    def _ask(self, text):
        with mock.patch("builtins.input", return_value=text):
            return install.confirm("q [y/N] ")

    def test_accepts_yes_words(self):
        for word in ("y", "Y", "yes", "YES", "oui", "Oui", " o ", " y "):
            self.assertTrue(self._ask(word), f"{word!r} should be yes")

    def test_rejects_everything_else(self):
        for word in ("", "n", "no", "bogus", "true", "1"):
            self.assertFalse(self._ask(word), f"{word!r} should be no")

    def test_eof_is_no_and_does_not_raise(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(install.confirm("q [y/N] "))

    def test_dry_run_never_prompts(self):
        install.DRY_RUN = True
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install.confirm("q [y/N] "))


class PlatformTests(unittest.TestCase):
    def test_windows(self):
        with mock.patch.object(install.os, "name", "nt"):
            self.assertEqual(install.detect_platform(), install.WINDOWS)

    def test_macos(self):
        with mock.patch.object(install.os, "name", "posix"), mock.patch.object(
            install.platform, "system", return_value="Darwin"
        ):
            self.assertEqual(install.detect_platform(), install.MACOS)

    def test_wsl(self):
        with mock.patch.object(install.os, "name", "posix"), mock.patch.object(
            install.platform, "system", return_value="Linux"
        ), mock.patch.object(
            install.platform,
            "release",
            return_value="5.15.90.1-microsoft-standard-WSL2",
        ):
            self.assertEqual(install.detect_platform(), install.WSL)

    def test_plain_linux(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            install.os, "name", "posix"
        ), mock.patch.object(
            install.platform, "system", return_value="Linux"
        ), mock.patch.object(
            install.platform, "release", return_value="6.8.0-45-generic"
        ):
            self.assertEqual(install.detect_platform(), install.LINUX)


class EnvTests(unittest.TestCase):
    def tearDown(self):
        install.PLATFORM = install.LINUX
        install.DRY_RUN = False

    def test_locale_spelling_per_platform(self):
        # C.utf8 is the Linux spelling; macOS uses C.UTF-8.
        install.PLATFORM = install.LINUX
        self.assertEqual(dict(install.env_values())["LANG"], "C.utf8")
        install.PLATFORM = install.MACOS
        self.assertEqual(dict(install.env_values())["LANG"], "C.UTF-8")

    def test_profile_path_linux_uses_invoking_user_home(self):
        # Not Path.home(): under sudo that would be /root.
        install.PLATFORM = install.LINUX
        with mock.patch.object(install, "user_home", return_value="/home/someone"):
            self.assertEqual(install.profile_path(), Path("/home/someone/.bashrc"))

    def test_profile_path_macos_zsh(self):
        install.PLATFORM = install.MACOS
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}), mock.patch.object(
            install, "user_home", return_value="/Users/x"
        ):
            self.assertEqual(install.profile_path(), Path("/Users/x/.zshrc"))

    def test_profile_path_macos_bash(self):
        install.PLATFORM = install.MACOS
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}), mock.patch.object(
            install, "user_home", return_value="/Users/x"
        ):
            self.assertEqual(install.profile_path(), Path("/Users/x/.bash_profile"))


class SetupEnvTests(unittest.TestCase):
    def tearDown(self):
        install.PLATFORM = install.LINUX
        install.DRY_RUN = False

    def _run(self, initial):
        install.PLATFORM = install.LINUX
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / ".bashrc"
            profile.write_text(initial)
            with mock.patch.object(install, "profile_path", return_value=profile):
                # setup_env prints banners; keep them out of the test output.
                with contextlib.redirect_stdout(io.StringIO()):
                    install.setup_env()
                    first = profile.read_text()
                    install.setup_env()  # a second run must change nothing
                    self.assertEqual(profile.read_text(), first)
            return first

    def test_appends_marker_and_variables(self):
        result = self._run("")
        self.assertIn(install.ENV_MARKER, result)
        self.assertIn('export SBX_NO_TELEMETRY="1"', result)
        self.assertEqual(result.count("SBX_NO_TELEMETRY"), 1)

    def test_is_idempotent(self):
        self.assertEqual(self._run("").count(install.ENV_MARKER), 1)

    def test_preserves_existing_profile_content(self):
        result = self._run('export LANG="en_US.UTF-8"\nexport PATH="$PATH:/opt/bin"\n')
        self.assertIn('export LANG="en_US.UTF-8"', result)
        self.assertIn('export PATH="$PATH:/opt/bin"', result)


if __name__ == "__main__":
    unittest.main()
