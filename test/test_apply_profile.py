"""Unit tests for docker-sbx-apply-profile.py and its update flow.

The command is a thin entry point over `main_update()` in
docker-sbx-create-sandbox.py; these tests cover both halves: the update flow
itself (which used to live in test_create_sandbox.py) and the delegation from
the command. Nothing here runs a real `sbx` or touches a real sandbox.
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, REPO / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cs = _load("docker_sbx_create_sandbox", "docker-sbx-create-sandbox.py")
ap = _load("docker_sbx_apply_profile", "docker-sbx-apply-profile.py")


class UpdateCommandTests(unittest.TestCase):
    """The update flow, reached through cs.main_update()."""

    def setUp(self):
        self._config = cs.CONFIG
        self._dry = cs.DRY_RUN
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.config_dir = self.root / "cfg"

    def _restore(self):
        cs.CONFIG = self._config
        cs.DRY_RUN = self._dry

    def _saved_config(
        self, name="s", profiles=(), profile_directories=(), profiles_dir="", project=None
    ):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / f"{name}.yaml"
        target.write_text(
            cs.render_config({
                "name": name,
                "project": str(project or self.project),
                "scan_secrets": "false",
                "profiles": list(profiles),
                "profile_directories": list(profile_directories),
                "profiles_dir": profiles_dir,
            })
        )
        return target

    def _patched_run(self):
        return (
            mock.patch.object(cs, "have", return_value=True),
            mock.patch.object(cs, "run", return_value=mock.Mock(returncode=0)),
            mock.patch.object(cs, "existing_sandbox_names", return_value=["s"]),
            mock.patch.object(cs, "wait_for_sandbox", return_value=True),
        )

    def test_requires_a_sandbox_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(["-profile-policy", "python", "--config-dir", str(self.config_dir)])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("you must provide the sandbox name with -n", out.getvalue())

    def test_requires_profile_names(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(["-n", "s", "--config-dir", str(self.config_dir)])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("at least one of -profile-policy or -profile-directory", err.getvalue())

    def test_rejects_unknown_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(["--apply-profile"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Unknown option", out.getvalue())

    def test_rejects_create_only_flags(self):
        # Argument errors are reported with the command's own usage, like the
        # create script's CliError handling.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(["-n", "s", "-profile-policy", "python", "-p", "/x"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(
            "Option -p cannot be used with the apply-profile command", out.getvalue()
        )

    def test_help_exits_zero_and_shows_only_update_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cs.main_update(["-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        for needle in ("-profile-policy", "-profile-directory", "-profiles-dir",
                       "--config-dir", "--no-config", "--dry-run"):
            self.assertIn(needle, text)
        self.assertNotIn("--recreate", text)

    def test_updates_without_creating_a_sandbox(self):
        have, run, names, wait = self._patched_run()
        with have, run, names, wait, mock.patch.object(
            cs, "apply_profiles"
        ) as apply, mock.patch.object(cs, "update_saved_config") as update, mock.patch.object(
            cs, "sbx"
        ) as fake_sbx, contextlib.redirect_stdout(io.StringIO()):
            rc = cs.main_update(
                ["-n", "s", "-profile-policy", "python,node",
                 "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(rc, 0)
        apply.assert_called_once_with()
        update.assert_called_once_with(["python", "node"], [])
        fake_sbx.assert_not_called()  # in particular, no `sbx create`

    def test_reads_the_saved_project_and_profile_directory(self):
        # Project-level assets need the project path, and the profile may live in
        # a directory recorded at creation, neither of which is on this command
        # line. Both must come from the saved config.
        profile_dir = self.root / "my-profiles"
        profile_dir.mkdir()
        (profile_dir / "node.yaml").write_text("name: node\n")
        self._saved_config(profiles_dir=str(profile_dir))
        have, run, names, wait = self._patched_run()
        with have, run, names, wait, mock.patch.object(
            cs, "apply_profiles"
        ), mock.patch.object(cs, "update_saved_config"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            rc = cs.main_update(
                ["-n", "s", "-profile-policy", "node",
                 "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(cs.CONFIG.path, str(self.project))
        self.assertEqual(cs.CONFIG.profiles_dir, str(profile_dir))

    def test_a_kit_bearing_profile_is_refused(self):
        # A kit is a create-time build context; applying it to a live sandbox
        # would silently do nothing, so the update path refuses it before it
        # touches the sandbox.
        profiles = self.root / "profiles"
        (profiles / "demo" / "kit").mkdir(parents=True)
        (profiles / "demo" / "profile.yaml").write_text(
            "name: demo\nkit: kit\npolicies:\n  network:\n    allow:\n      - pypi.org\n"
        )
        (profiles / "demo" / "kit" / "demo.yaml").write_text(
            "# syntax=docker/sandbox-kit:3\nschemaVersion: \"3\"\nkind: mixin\n"
        )
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(
                ["-n", "s", "-profile-directory", "demo",
                 "-profiles-dir", str(profiles),
                 "--dry-run", "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("includes a sandbox kit", err.getvalue())
        self.assertIn("only be used when the sandbox is created", err.getvalue())

    def test_a_missing_sandbox_aborts(self):
        err = io.StringIO()
        with mock.patch.object(cs, "have", return_value=True), mock.patch.object(
            cs, "run", return_value=mock.Mock(returncode=0)
        ), mock.patch.object(
            cs, "existing_sandbox_names", return_value=["other"]
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(
                ["-n", "s", "-profile-policy", "python",
                 "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("does not exist", err.getvalue())

    def test_a_failed_sbx_query_is_not_reported_as_a_missing_sandbox(self):
        err = io.StringIO()
        with mock.patch.object(cs, "have", return_value=True), mock.patch.object(
            cs, "run", return_value=mock.Mock(returncode=1)
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit) as ctx:
            cs.main_update(
                ["-n", "s", "-profile-policy", "python",
                 "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("could not query sbx", err.getvalue())

    def test_dry_run_reports_without_querying_sbx(self):
        out = io.StringIO()
        with mock.patch.object(cs, "have") as have, mock.patch.object(
            cs, "run"
        ) as run, mock.patch.object(cs, "apply_profiles"), mock.patch.object(
            cs, "update_saved_config"
        ), contextlib.redirect_stdout(out):
            rc = cs.main_update(
                ["-n", "s", "-profile-policy", "python", "--dry-run",
                 "--config-dir", str(self.config_dir)]
            )
        self.assertEqual(rc, 0)
        have.assert_not_called()
        run.assert_not_called()
        self.assertIn("profile update (dry-run)", out.getvalue())


class EntryPointTests(unittest.TestCase):
    """docker-sbx-apply-profile.py: loading the implementation and delegating."""

    def test_delegates_argv_to_main_update(self):
        argv = ["-n", "s", "-profile-policy", "node", "--dry-run"]
        with mock.patch.object(
            ap, "load_create_module", return_value=cs
        ), mock.patch.object(cs, "main_update", return_value=0) as update:
            rc = ap.main(argv)
        self.assertEqual(rc, 0)
        update.assert_called_once_with(argv)

    def test_propagates_the_exit_code(self):
        with mock.patch.object(
            ap, "load_create_module", return_value=cs
        ), mock.patch.object(cs, "main_update", return_value=3):
            self.assertEqual(ap.main(["-h"]), 3)

    def test_a_missing_implementation_is_reported(self):
        original = ap.__file__
        self.addCleanup(setattr, ap, "__file__", original)
        ap.__file__ = str(Path(tempfile.mkdtemp()) / "docker-sbx-apply-profile.py")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(ap.load_create_module())
        self.assertIn("was not found", err.getvalue())

    def test_a_missing_implementation_makes_main_fail(self):
        with mock.patch.object(ap, "load_create_module", return_value=None):
            self.assertEqual(ap.main([]), 1)


if __name__ == "__main__":
    unittest.main()
