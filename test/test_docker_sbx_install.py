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


class WingetDockerHelpTests(unittest.TestCase):
    """A failed `winget install Docker.DockerDesktop` must explain the cleanup.

    Windows only, and only on failure: the happy path has to stay silent, and the
    dry run must not trip over the stubbed zero exit status.
    """

    def setUp(self):
        self._platform = install.PLATFORM
        install.PLATFORM = install.WINDOWS
        self.addCleanup(self._restore)

    def _restore(self):
        install.PLATFORM = self._platform
        install.have = self._have
        install.run = self._run

    def _install(self, run_result, winget_present=True):
        """Drive install_docker_desktop with winget faked; return (code, stdout, stderr)."""
        self._have, self._run = install.have, install.run
        install.have = lambda command: winget_present
        install.run = lambda cmd, **kwargs: run_result
        out, err = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                install.install_docker_desktop()
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_failure_prints_cleanup_then_manual_install_link(self):
        code, text, err = self._install(mock.Mock(returncode=0x8A15000F))
        self.assertEqual(code, 1)
        # the winget cleanup comes first ...
        self.assertIn("winget source reset --force", text)
        self.assertIn("0x8a15000f", text)
        # ... and the manual installer is the fallback when it does not help.
        self.assertIn(install.DOCKER_DESKTOP_WINDOWS_DOCS, text)
        self.assertIn("IF IT STILL DOES NOT WORK", text)
        # Cleanup must be described before the fallback, not the other way round.
        self.assertLess(
            text.index("winget source reset"), text.index("IF IT STILL DOES NOT WORK")
        )
        # The manual install is useless without a fresh shell: the installer's PATH
        # change does not reach a terminal that was already open.
        self.assertGreater(
            text.index("CLOSE THIS TERMINAL AND OPEN A NEW ONE"),
            text.index(install.DOCKER_DESKTOP_WINDOWS_DOCS),
        )
        self.assertIn("Error:", err)

    def test_success_is_silent(self):
        code, text, err = self._install(mock.Mock(returncode=0))
        self.assertIsNone(code)
        self.assertEqual(text, "")
        self.assertEqual(err, "")

    def test_missing_winget_points_at_the_manual_installer(self):
        # No winget at all: the message goes through die(), i.e. to stderr.
        code, text, err = self._install(None, winget_present=False)
        self.assertEqual(code, 1)
        self.assertEqual(text, "")
        self.assertIn(install.DOCKER_DESKTOP_WINDOWS_DOCS, err)
        self.assertIn("restart your terminal", err)


class RequireAdminTests(unittest.TestCase):
    """Windows must not demand an elevated shell; Linux/WSL still must."""

    def _run_gate(self, platform, admin):
        out = io.StringIO()
        code = None
        with mock.patch.object(install, "PLATFORM", platform), mock.patch.object(
            install, "is_admin", return_value=admin
        ):
            try:
                with contextlib.redirect_stdout(out):
                    install.require_admin()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_windows_regular_user_is_allowed(self):
        # Docker Desktop's and sbx's installers raise their own UAC prompt, so the
        # launching shell needs no elevation here.
        code, _ = self._run_gate(install.WINDOWS, admin=False)
        self.assertIsNone(code)

    def test_linux_without_root_still_exits(self):
        for platform in (install.LINUX, install.WSL):
            code, text = self._run_gate(platform, admin=False)
            self.assertEqual(code, 1, platform)
            self.assertIn("must be run as root", text)

    def test_linux_with_root_is_allowed(self):
        code, _ = self._run_gate(install.LINUX, admin=True)
        self.assertIsNone(code)


class HypervisorPlatformTests(unittest.TestCase):
    """The one Windows step that needs elevation - probed, then demanded only if missing."""

    def _probe(self, returncode, stdout):
        with mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=returncode, stdout=stdout)
        ), contextlib.redirect_stdout(io.StringIO()):
            return install.hypervisor_platform_state()

    def test_probe_does_not_use_the_elevated_cmdlet(self):
        # Get-WindowsOptionalFeature -Online needs admin rights even to READ, so it
        # fails "Access is denied" for a regular user and made an enabled feature
        # look missing. The probe must stay on the unelevated Win32_OptionalFeature.
        with mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=0, stdout="1")
        ) as fake_run, contextlib.redirect_stdout(io.StringIO()):
            install.hypervisor_platform_state()
        command = " ".join(fake_run.call_args[0][0])
        self.assertIn("Win32_OptionalFeature", command)
        self.assertNotIn("Get-WindowsOptionalFeature", command)
        self.assertNotIn('"', command)  # no doubled quoting through the Windows argv rules

    def test_probe_maps_install_state_codes(self):
        self.assertEqual(self._probe(0, "1\n"), "Enabled")
        self.assertEqual(self._probe(0, "2\n"), "Disabled")
        self.assertEqual(self._probe(0, "3\n"), "Absent")
        self.assertEqual(self._probe(0, "4\n"), "Unknown")

    def test_probe_passes_through_unknown_output(self):
        # Raw, not a bool: it is the only evidence when the answer is surprising.
        self.assertEqual(self._probe(0, "Enabled\n"), "Enabled")
        self.assertEqual(self._probe(0, "something localized\n"), "something localized")

    def test_failed_probe_returns_empty(self):
        self.assertEqual(self._probe(1, "boom"), "")

    def test_enabled_states(self):
        for state in ("Enabled", "enabled", " EnablePending "):
            self.assertTrue(install.hypervisor_state_is_enabled(state), state)
        for state in ("Disabled", "DisabledWithPayloadRemoved", ""):
            self.assertFalse(install.hypervisor_state_is_enabled(state), state)

    def _enable(self, state, admin, dry_run=False):
        out = io.StringIO()
        code = None
        with mock.patch.object(install, "DRY_RUN", dry_run), mock.patch.object(
            install, "hypervisor_platform_state", return_value=state
        ), mock.patch.object(install, "is_admin", return_value=admin), mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=0, stdout="")
        ) as fake_run:
            try:
                with contextlib.redirect_stdout(out):
                    install.enable_hypervisor_platform()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), fake_run

    def test_already_enabled_does_nothing(self):
        code, text, fake_run = self._enable("Enabled", admin=False)
        self.assertIsNone(code)
        self.assertIn("already enabled", text)
        fake_run.assert_not_called()

    def test_enable_pending_counts_as_on_and_asks_for_a_restart(self):
        # Enabling it again would not help - only a restart would.
        code, text, fake_run = self._enable("EnablePending", admin=False)
        self.assertIsNone(code)
        self.assertIn("Restart Windows before starting a local sandbox", text)
        fake_run.assert_not_called()

    def test_missing_and_elevated_enables_it_without_prompting(self):
        code, _, fake_run = self._enable("Disabled", admin=True)
        self.assertIsNone(code)
        argv = " ".join(fake_run.call_args[0][0])
        self.assertIn(
            f"Enable-WindowsOptionalFeature -Online -FeatureName {install.HYPERVISOR_FEATURE}", argv
        )
        self.assertIn("-NoRestart", argv)  # never block on the "restart now?" prompt

    def test_missing_and_not_elevated_gives_the_one_command(self):
        code, text, fake_run = self._enable("Disabled", admin=False)
        self.assertEqual(code, 1)
        self.assertIn("Run as administrator", text)
        self.assertIn(f"Enable-WindowsOptionalFeature -Online -FeatureName {install.HYPERVISOR_FEATURE}", text)
        fake_run.assert_not_called()  # nothing is attempted behind the user's back

    def test_the_observed_state_is_shown(self):
        code, text, _ = self._enable("DisabledWithPayloadRemoved", admin=False)
        self.assertEqual(code, 1)
        self.assertIn("DisabledWithPayloadRemoved", text)

    def test_unreadable_state_does_not_block_the_install(self):
        # A failed probe says nothing about the feature; blocking on it is what told
        # an already-enabled machine to go enable it.
        code, text, fake_run = self._enable("", admin=False)
        self.assertIsNone(code)
        self.assertIn("ELEVATED PowerShell", text)  # tells the user how to check by hand
        fake_run.assert_not_called()

    def test_absent_is_treated_as_not_enabled(self):
        code, text, _ = self._enable("Absent", admin=False)
        self.assertEqual(code, 1)
        self.assertIn("Absent", text)

    def test_hyperv_is_not_mistaken_for_whpx(self):
        # The exact confusion this step runs into: Hyper-V/WSL2 is a different feature.
        _, text, _ = self._enable("Disabled", admin=False)
        self.assertIn("not Hyper-V", text)

    def test_dry_run_probes_nothing(self):
        with mock.patch.object(install, "DRY_RUN", True), mock.patch.object(
            install, "hypervisor_platform_state"
        ) as probe, mock.patch.object(install, "run") as fake_run:
            with contextlib.redirect_stdout(io.StringIO()):
                install.enable_hypervisor_platform()
        probe.assert_not_called()
        fake_run.assert_not_called()


class AdminGateInMainTests(unittest.TestCase):
    """--dry-run changes nothing, so main() must not gate it behind privilege."""

    def setUp(self):
        self._dry, self._platform = install.DRY_RUN, install.PLATFORM
        self.addCleanup(self._restore)

    def _restore(self):
        install.DRY_RUN, install.PLATFORM = self._dry, self._platform

    def _main(self, argv):
        with mock.patch.object(install, "require_admin") as gate, mock.patch.object(
            install, "detect_platform", return_value=install.WINDOWS
        ), mock.patch.object(install, "install_or_update_docker"), mock.patch.object(
            install, "install_sbx"
        ), mock.patch.object(install, "sbx_login"), mock.patch.object(
            install, "setup_sbx_policy"
        ), mock.patch.object(install, "setup_env"):
            with contextlib.redirect_stdout(io.StringIO()):
                code = install.main(argv)
        return code, gate

    def test_dry_run_does_not_call_require_admin(self):
        code, gate = self._main(["--dry-run"])
        self.assertEqual(code, 0)
        gate.assert_not_called()

    def test_real_run_still_calls_require_admin(self):
        code, gate = self._main([])
        self.assertEqual(code, 0)
        gate.assert_called_once()


class DockerDetectionTests(unittest.TestCase):
    """Docker Desktop can be installed yet invisible to a shell started earlier.

    `have("docker")` is a PATH-only test, which reported a machine that already had
    Docker Desktop as "not installed" and offered to install it again.
    """

    def _absent(self, **env):
        return mock.patch.object(install.shutil, "which", return_value=None), mock.patch.dict(
            os.environ, env
        )

    def test_path_hit_wins(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/docker"):
            self.assertEqual(install.docker_binary(), "/usr/bin/docker")
        self.assertTrue(install.docker_installed())

    def test_windows_finds_docker_off_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            which, env = self._absent(
                LOCALAPPDATA=tmp, ProgramFiles="", ProgramW6432="", **{"ProgramFiles(x86)": ""}
            )
            with which, env, mock.patch.object(install, "PLATFORM", install.WINDOWS):
                self.assertEqual(install.docker_binary(), str(exe))
                self.assertTrue(install.docker_installed())

    def test_absent_everywhere(self):
        with mock.patch.object(install.shutil, "which", return_value=None), mock.patch.object(
            install, "PLATFORM", install.LINUX
        ):
            self.assertEqual(install.docker_binary(), "")
            self.assertFalse(install.docker_installed())

    def test_install_dirs_are_not_scanned_off_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Programs" / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            which, env = self._absent(LOCALAPPDATA=tmp)
            with which, env, mock.patch.object(install, "PLATFORM", install.MACOS):
                self.assertEqual(install.docker_binary(), "")

    def test_stale_path_warns_instead_of_offering_an_install(self):
        err = io.StringIO()
        with mock.patch.object(
            install, "docker_binary", return_value=r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        ), mock.patch.object(install.shutil, "which", return_value=None), mock.patch.object(
            install, "docker_version", return_value="27.0.0"
        ), mock.patch.object(install, "install_docker") as fake_install, mock.patch.object(
            install, "confirm", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            install.install_or_update_docker()
        fake_install.assert_not_called()  # Docker is there; do not reinstall it
        self.assertIn("not on this shell's PATH", err.getvalue())


class WingetInstallTests(unittest.TestCase):
    """An up-to-date package makes winget exit non-zero - that is not a failure.

    0x8A15002B (UPDATE_NOT_APPLICABLE) used to abort the whole installation with
    "command failed with exit code 2316632107", and for Docker Desktop it printed
    the recovery guide at a machine that needed no recovery.
    """

    def _winget_install(self, returncode, silent=False):
        out = io.StringIO()
        with mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=returncode)
        ) as fake_run, contextlib.redirect_stdout(out):
            code = install.winget_install("Pkg.Id", silent=silent)
        return code, out.getvalue(), fake_run

    def test_already_installed_and_up_to_date_is_success(self):
        code, text, _ = self._winget_install(install.WINGET_ALREADY_SATISFIED)
        self.assertEqual(code, 0)
        self.assertIn("already installed and up to date", text)

    def test_other_codes_are_passed_through(self):
        code, _, _ = self._winget_install(0x8A15000F)
        self.assertEqual(code, 0x8A15000F)

    def test_silent_flag_reaches_winget(self):
        _, _, fake_run = self._winget_install(0, silent=True)
        argv = fake_run.call_args[0][0]
        self.assertIn("-h", argv)
        self.assertIn("Pkg.Id", argv)

    def test_satisfied_docker_desktop_does_not_show_the_recovery_guide(self):
        with mock.patch.object(install, "PLATFORM", install.WINDOWS), mock.patch.object(
            install, "have", return_value=True
        ), mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=install.WINGET_ALREADY_SATISFIED)
        ), contextlib.redirect_stdout(io.StringIO()):
            install.install_docker_desktop()  # must return, not SystemExit

    def test_satisfied_sbx_still_enables_the_hypervisor_platform(self):
        with mock.patch.object(install, "PLATFORM", install.WINDOWS), mock.patch.object(
            install, "have", return_value=True
        ), mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=install.WINGET_ALREADY_SATISFIED)
        ), mock.patch.object(
            install, "enable_hypervisor_platform"
        ) as hypervisor, contextlib.redirect_stdout(io.StringIO()):
            install.install_sbx()
        hypervisor.assert_called_once()

    def test_a_real_sbx_install_failure_still_stops_it(self):
        with mock.patch.object(install, "PLATFORM", install.WINDOWS), mock.patch.object(
            install, "have", return_value=True
        ), mock.patch.object(
            install, "run", return_value=mock.Mock(returncode=0x8A15000F)
        ), mock.patch.object(install, "enable_hypervisor_platform") as hypervisor:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit) as ctx:
                install.install_sbx()
        self.assertEqual(ctx.exception.code, 1)
        hypervisor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
