#!/usr/bin/env python3
"""Unit tests for docker-sbx-create-sandbox.py (standard library only).

Covers the pure helpers the CLI relies on: argument parsing, the env-file reader,
volume specs, Slack validation, host extraction, the secrets-file scanner, the
minimal YAML profile parser, the sandbox-name pre-check and the wait for a new
sandbox's first start. The end-to-end behaviour is covered separately by
test/run-tests.sh, which drives this implementation's CLI.

    python3 -m unittest discover -s test -p 'test_*.py'
    (or, from inside test/:  python3 -m unittest test_create_sandbox)
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The module name has hyphens, so it cannot be imported by name. The script sits
# in the repository root, one level up from this test/ directory.
REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "docker_sbx_create_sandbox", REPO / "docker-sbx-create-sandbox.py"
)
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)


class CliTests(unittest.TestCase):
    def test_defaults(self):
        cfg = cs.parse_args(["-n", "demo", "-p", "/tmp/x"])
        self.assertEqual(cfg.name, "demo")
        self.assertEqual(cfg.path, "/tmp/x")
        self.assertEqual(cfg.secrets, "true")
        self.assertEqual(cfg.volumes, [])
        self.assertFalse(cfg.dry_run)

    def test_repeatable_volumes(self):
        cfg = cs.parse_args(["-n", "d", "-p", "/x", "-v", "/a", "-v", "/b:rw"])
        self.assertEqual(cfg.volumes, ["/a", "/b:rw"])

    def test_all_value_options(self):
        cfg = cs.parse_args(
            ["-n", "d", "-p", "/x", "-e", "f.env", "-s", "false", "-sf", "s.json",
             "-profile", "python", "-profile-directory", "/p"]
        )
        self.assertEqual(
            (cfg.envfile, cfg.secrets, cfg.secretsfile, cfg.profiles, cfg.profile_directory),
            ("f.env", "false", "s.json", "python", "/p"),
        )

    def test_help_flags(self):
        self.assertTrue(cs.parse_args(["-h"]).show_help)
        self.assertTrue(cs.parse_args(["--help"]).show_help)

    def test_dry_run_flag(self):
        self.assertTrue(cs.parse_args(["--dry-run", "-n", "d", "-p", "/x"]).dry_run)

    def test_unknown_option_raises(self):
        with self.assertRaises(cs.CliError) as caught:
            cs.parse_args(["--bogus"])
        self.assertIn("Unknown option: --bogus", str(caught.exception))

    def test_the_removed_apt_flag_is_rejected_rather_than_ignored(self):
        # `-u` used to gate an apt upgrade inside the sandbox; that apt run broke
        # sandboxes that had not finished starting, so it is gone rather than a
        # silently accepted no-op.
        with self.assertRaises(cs.CliError) as caught:
            cs.parse_args(["-n", "d", "-p", "/x", "-u", "true"])
        self.assertIn("-u", str(caught.exception))

    def test_option_without_value_raises(self):
        with self.assertRaises(cs.CliError):
            cs.parse_args(["-n"])

    def test_usage_text_mentions_the_documented_flags(self):
        text = cs.usage_text()
        for needle in ("Usage:", "-sf", "-gsf", "-profile", "-v path", "--dry-run"):
            self.assertIn(needle, text)


class UnquoteTests(unittest.TestCase):
    def test_strips_whitespace_and_one_quote_pair(self):
        cases = {
            "  spaced  ": "spaced",
            '"double"': "double",
            "'single'": "single",
            '"mismatched\'': "mismatched",
            "plain": "plain",
            "": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(cs.unquote(raw), expected, raw)


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.envfile = Path(self._tmp.name) / "sbx.env"
        cs.CONFIG.envfile = str(self.envfile)

    def tearDown(self):
        self._tmp.cleanup()
        cs.CONFIG.envfile = ""

    def test_last_assignment_wins(self):
        self.envfile.write_text("KEY=first\nKEY=second\n")
        self.assertEqual(cs.env_file_get("KEY"), "second")

    def test_value_keeps_equals_signs(self):
        self.envfile.write_text("TOKEN=a=b=c\n")
        self.assertEqual(cs.env_file_get("TOKEN"), "a=b=c")

    def test_missing_key_is_empty(self):
        self.envfile.write_text("OTHER=1\n")
        self.assertEqual(cs.env_file_get("KEY"), "")

    def test_no_env_file_is_empty(self):
        cs.CONFIG.envfile = str(Path(self._tmp.name) / "absent.env")
        self.assertEqual(cs.env_file_get("KEY"), "")

    def test_slack_enabled_is_exact(self):
        self.envfile.write_text("SBX_SLACK_ENABLED=true\n")
        self.assertTrue(cs.slack_enabled())
        self.envfile.write_text("SBX_SLACK_ENABLED=True\n")
        self.assertFalse(cs.slack_enabled())

    def test_slack_events_default(self):
        self.envfile.write_text("X=1\n")
        self.assertEqual(cs.slack_events(), cs.DEFAULT_SLACK_EVENTS)


class VolumeSpecTests(unittest.TestCase):
    def test_modes(self):
        cases = {
            "/a/b": ("/a/b", "ro"),
            "/a/b:ro": ("/a/b", "ro"),
            "/a/b:rw": ("/a/b", "rw"),
            "/a/b:RW": ("/a/b", "rw"),
            "/a/b:RO": ("/a/b", "ro"),
            "C:\\docs": ("C:\\docs", "ro"),          # a drive letter is not a suffix
            "C:\\docs:rw": ("C:\\docs", "rw"),
        }
        for spec, expected in cases.items():
            self.assertEqual(cs.parse_volume_spec(spec), expected, spec)


class HostFromUrlTests(unittest.TestCase):
    def test_extraction(self):
        cases = {
            "https://hooks.slack.com/services/T/B/x": "hooks.slack.com",
            "http://host.docker.internal:11434": "host.docker.internal",
            "https://api.example.com": "api.example.com",
            "hooks.slack.com": "hooks.slack.com",
            "": "",
        }
        for url, expected in cases.items():
            self.assertEqual(cs.host_from_url(url), expected, url)


class SlackValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.envfile = Path(self._tmp.name) / "sbx.env"
        cs.CONFIG.envfile = str(self.envfile)
        cs.CONFIG.name = "probe"

    def tearDown(self):
        self._tmp.cleanup()
        cs.CONFIG.envfile = ""
        cs.CONFIG.name = ""

    def _run(self, body):
        self.envfile.write_text(body)
        # die() writes to stderr; keep the failure-path tests quiet.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cs.validate_slack_config()

    def test_disabled_is_a_no_op(self):
        self._run("SBX_SLACK_ENABLED=false\n")

    def test_webhook_alone_is_enough(self):
        self._run("SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n")

    def test_token_and_channel_are_enough(self):
        self._run("SBX_SLACK_ENABLED=true\nSBX_SLACK_BOT_TOKEN=x\nSBX_SLACK_CHANNEL=C1\n")

    def test_enabled_without_target_aborts(self):
        with self.assertRaises(SystemExit):
            self._run("SBX_SLACK_ENABLED=true\n")

    def test_token_without_channel_aborts(self):
        with self.assertRaises(SystemExit):
            self._run("SBX_SLACK_ENABLED=true\nSBX_SLACK_BOT_TOKEN=x\n")

    def test_unknown_event_aborts(self):
        with self.assertRaises(SystemExit):
            self._run(
                "SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n"
                "SBX_SLACK_EVENTS=Stop,Bogus\n"
            )

    def test_all_supported_events_accepted(self):
        self._run(
            "SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n"
            "SBX_SLACK_EVENTS=" + ",".join(cs.SUPPORTED_SLACK_EVENTS) + "\n"
        )


class SecretsConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_when_absent(self):
        search, avoid = cs.read_secrets_config("")
        self.assertEqual(tuple(search), cs.DEFAULT_SEARCH_PATTERNS)
        self.assertEqual(tuple(avoid), cs.DEFAULT_AVOID_PATTERNS)

    def test_custom_config_is_read(self):
        path = Path(self._tmp.name) / "s.json"
        path.write_text(json.dumps({"search": ["*.env"], "avoid": ["vendor"]}))
        self.assertEqual(cs.read_secrets_config(str(path)), (["*.env"], ["vendor"]))

    def test_malformed_config_aborts(self):
        for body in ('["not an object"]', '{"search": "nope"}', "not json at all"):
            path = Path(self._tmp.name) / "bad.json"
            path.write_text(body)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cs.read_secrets_config(str(path))

    def test_generate_secrets_file_content(self):
        target = Path(self._tmp.name) / "gen.json"
        with contextlib.redirect_stdout(io.StringIO()):
            cs.generate_secrets_file(str(target))
        data = json.loads(target.read_text())
        self.assertEqual(data["search"], list(cs.DEFAULT_SEARCH_PATTERNS))
        self.assertEqual(data["avoid"], list(cs.DEFAULT_AVOID_PATTERNS))


class SecretScannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "node_modules").mkdir()
        (self.root / ".venv").mkdir()
        (self.root / "app.jks").touch()
        (self.root / "sub" / "key.properties").touch()
        (self.root / "sub" / "keep.txt").touch()
        (self.root / "node_modules" / "creds.key").touch()
        (self.root / ".venv" / "creds.key").touch()

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_matches_and_prunes_avoided_trees(self):
        found = cs.find_secret_files(
            str(self.root), cs.DEFAULT_SEARCH_PATTERNS, cs.DEFAULT_AVOID_PATTERNS
        )
        names = sorted(Path(f).name for f in found)
        self.assertEqual(names, ["app.jks", "key.properties"])

    def test_result_is_sorted_and_deterministic(self):
        args = (str(self.root), cs.DEFAULT_SEARCH_PATTERNS, cs.DEFAULT_AVOID_PATTERNS)
        self.assertEqual(cs.find_secret_files(*args), cs.find_secret_files(*args))

    def test_matching_is_case_sensitive(self):
        (self.root / "UPPER.JKS").touch()
        found = cs.find_secret_files(
            str(self.root), cs.DEFAULT_SEARCH_PATTERNS, cs.DEFAULT_AVOID_PATTERNS
        )
        self.assertNotIn("UPPER.JKS", [Path(f).name for f in found])

    def test_empty_patterns_find_nothing(self):
        self.assertEqual(cs.find_secret_files(str(self.root), [], []), [])

    def test_git_dir_discovery(self):
        self.assertEqual(cs.find_git_dir(str(self.root)), "")
        (self.root / ".git").mkdir()
        self.assertEqual(cs.find_git_dir(str(self.root)), str(self.root / ".git"))

    def test_git_dir_discovery_nested(self):
        nested = self.root / "sub" / "proj"
        nested.mkdir()
        (nested / ".git").mkdir()
        self.assertEqual(cs.find_git_dir(str(self.root)), str(nested / ".git"))


class SecretsScanTests(unittest.TestCase):
    """check_project_secrets_files() reads the -sf file through the module globals.

    It called a `secretsfile()` accessor that was never defined, so every default
    run died with NameError the moment the secret scan started - and the `-sf`
    option never reached read_secrets_config at all, since nothing wired it up.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._config = cs.CONFIG
        self.addCleanup(self._restore)

    def _restore(self):
        cs.CONFIG = self._config
        self._tmp.cleanup()

    def _configure(self, *extra):
        cs.CONFIG = cs.parse_args(["-n", "d", "-p", str(self.root), *extra])

    def test_accessor_exposes_the_configured_file(self):
        self._configure("-sf", "custom.json")
        self.assertEqual(cs.secretsfile(), "custom.json")

    def test_missing_sf_file_falls_back_to_the_defaults(self):
        # main() rejects a missing -sf before getting here; called directly, the
        # reader must degrade to the built-in patterns rather than to nothing.
        self._configure("-sf", str(self.root / "missing.json"))
        search, avoid = cs.read_secrets_config(cs.secretsfile())
        self.assertEqual(tuple(search), cs.DEFAULT_SEARCH_PATTERNS)
        self.assertEqual(tuple(avoid), cs.DEFAULT_AVOID_PATTERNS)

    def test_clean_project_scans_without_raising(self):
        self._configure()
        (self.root / "src.py").write_text("")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cs.check_project_secrets_files()  # this used to be a NameError
        self.assertIn("No secrets files detected", out.getvalue())

    def test_the_sf_file_actually_drives_the_scan(self):
        # Because the accessor was missing, -sf was silently ignored as well.
        config = self.root / "s.json"
        config.write_text(json.dumps({"search": ["*.token"], "avoid": []}))
        (self.root / "creds.token").write_text("")
        self._configure("-sf", str(config))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit):
            cs.check_project_secrets_files()

    def test_default_patterns_still_apply(self):
        (self.root / "app.jks").touch()
        self._configure()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit):
            cs.check_project_secrets_files()


class SandboxListTests(unittest.TestCase):
    """Sandbox names for the "already exists" check.

    parse_table_names mirrors docker-sbx-update-sandboxes.py, so the two scripts
    agree on what a name in `sbx ls`'s first column looks like.
    """

    def test_parses_the_first_column_of_the_human_table(self):
        text = (
            "SANDBOX       AGENT    STATUS    PORTS   WORKSPACE   CREATED\n"
            "docker-sbx    claude   running           c:\\p        7h\n"
            "sequoia-420   claude   stopped           C:\\u        1h\n"
        )
        self.assertEqual(cs.parse_table_names(text), ["docker-sbx", "sequoia-420"])

    def test_headers_dividers_and_blanks_are_not_names(self):
        self.assertEqual(cs.parse_table_names("NAME\n----\n\n"), [])

    def test_names_only_output_wins_when_it_answers(self):
        with mock.patch.object(cs, "run") as fake_run:
            fake_run.return_value = mock.Mock(returncode=0, stdout="a\nb\n")
            self.assertEqual(cs.existing_sandbox_names(), ["a", "b"])
            self.assertEqual(fake_run.call_args[0][0], ["sbx", "ls", "-q"])

    def test_empty_names_only_output_falls_back_to_the_table(self):
        with mock.patch.object(cs, "run") as fake_run:
            fake_run.side_effect = [
                mock.Mock(returncode=0, stdout=""),
                mock.Mock(returncode=0, stdout="SANDBOX\nx\n"),
            ]
            self.assertEqual(cs.existing_sandbox_names(), ["x"])


class CreateSandboxTests(unittest.TestCase):
    """A failed `sbx create` used to scroll past; an existing name is not reused."""

    def setUp(self):
        self._config = cs.CONFIG
        cs.CONFIG = cs.parse_args(["-n", "d", "-p", "/x"])
        self.addCleanup(self._restore)

    def _restore(self):
        cs.CONFIG = self._config

    def test_create_is_fatal_when_sbx_fails(self):
        with mock.patch.object(cs, "existing_sandbox_names", return_value=[]), mock.patch.object(
            cs, "sbx"
        ) as fake_sbx:
            cs.create_sandbox(["/docs:ro"])
        argv, kwargs = fake_sbx.call_args
        self.assertEqual(argv[0][:4], ["create", "--name", "d", "claude"])
        self.assertEqual(argv[0][-1], "/docs:ro")
        self.assertTrue(kwargs["check"])

    def test_an_existing_name_is_refused_before_anything_is_created(self):
        err = io.StringIO()
        with mock.patch.object(
            cs, "existing_sandbox_names", return_value=["d"]
        ), mock.patch.object(cs, "sbx") as fake_sbx, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cs.create_sandbox([])
        fake_sbx.assert_not_called()
        self.assertIn("sbx rm d", err.getvalue())


class SandboxReadinessTests(unittest.TestCase):
    """Setup must not configure a sandbox whose first start is still running.

    The first start is when the guest gets its agent, and the previous code ran the
    setup straight after `sbx create` - into a sandbox mid-boot, which left ones
    that could never be started again.
    """

    def setUp(self):
        self._config = cs.CONFIG
        self._dry = cs.DRY_RUN
        cs.DRY_RUN = False
        cs.CONFIG = cs.parse_args(["-n", "d", "-p", "/x"])
        self.addCleanup(self._restore)

    def _restore(self):
        cs.CONFIG = self._config
        cs.DRY_RUN = self._dry

    def test_the_probe_execs_in_and_tests_the_agent_binary(self):
        with mock.patch.object(cs, "run") as fake_run:
            fake_run.return_value = mock.Mock(returncode=0)
            self.assertTrue(cs.sandbox_ready())
            argv = fake_run.call_args[0][0]
        self.assertEqual(argv[:3], ["sbx", "exec", "-i"])
        self.assertIn("d", argv)
        self.assertIn(".local/bin/claude", argv[-1])

    def test_a_failing_probe_is_not_ready(self):
        with mock.patch.object(cs, "run") as fake_run:
            fake_run.return_value = mock.Mock(returncode=1)
            self.assertFalse(cs.sandbox_ready())

    def test_dry_run_does_not_probe_or_wait(self):
        cs.DRY_RUN = True
        with mock.patch.object(cs, "sandbox_ready") as probe:
            self.assertTrue(cs.wait_for_sandbox())
        probe.assert_not_called()

    def test_waiting_stops_at_the_first_success(self):
        with mock.patch.object(cs, "sandbox_ready", side_effect=[False, True]), mock.patch.object(
            cs.time, "sleep"
        ) as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cs.wait_for_sandbox())
        self.assertEqual(sleep.call_count, 1)

    def test_waiting_gives_up_at_the_timeout(self):
        # monotonic: the loop start, then past the deadline; the probe never passes.
        clock = iter([0.0, 0.0, cs.SANDBOX_READY_TIMEOUT + 1])
        with mock.patch.object(cs, "sandbox_ready", return_value=False), mock.patch.object(
            cs.time, "monotonic", side_effect=clock
        ), mock.patch.object(cs.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cs.wait_for_sandbox())

    def _setup_calls(self, *extra):
        """Every `sbx` argv setup_sandbox() issues, with the wait stubbed out."""
        with tempfile.TemporaryDirectory() as tmp:
            cs.CONFIG = cs.parse_args(["-n", "d", "-p", tmp, *extra])
            with mock.patch.object(cs, "sbx") as fake_sbx, mock.patch.object(
                cs, "wait_for_sandbox", return_value=True
            ):
                cs.setup_sandbox()
            return [call[0][0] for call in fake_sbx.call_args_list], tmp

    def test_the_sandbox_is_started_before_anything_runs_inside_it(self):
        calls, _ = self._setup_calls()
        self.assertEqual(calls[0], ["start", "d"])
        self.assertTrue(any("exec" in call for call in calls))

    def test_no_env_file_is_passed_as_a_dash_not_an_empty_argument(self):
        # An empty positional does not survive `sbx exec` reliably, so the tail is
        # <path> -.
        calls, tmp = self._setup_calls()
        bash = [call for call in calls if "bash" in call][0]
        self.assertEqual(bash[-2], tmp)
        self.assertEqual(bash[-1], "-")

    def test_env_file_path_comes_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "sbx.env"
            env.write_text("ANTHROPIC_API_KEY=x\n")
            cs.CONFIG = cs.parse_args(["-n", "d", "-p", tmp, "-e", str(env)])
            with mock.patch.object(cs, "sbx") as fake_sbx, mock.patch.object(
                cs, "wait_for_sandbox", return_value=True
            ):
                cs.setup_sandbox()
            calls = [call[0][0] for call in fake_sbx.call_args_list]
        bash = [call for call in calls if "bash" in call][0]
        self.assertEqual(bash[-2], tmp)  # project path first
        self.assertEqual(bash[-1], "/home/agent/sbx.env")  # env-file path second
        self.assertTrue(any("cp" in call for call in calls))

    def test_a_sandbox_that_never_starts_is_not_configured(self):
        err = io.StringIO()
        with mock.patch.object(cs, "sbx") as fake_sbx, mock.patch.object(
            cs, "wait_for_sandbox", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit):
            cs.setup_sandbox()
        self.assertFalse([call for call in fake_sbx.call_args_list if "exec" in call[0][0]])
        self.assertIn("did not start", err.getvalue())


class RunHelperTests(unittest.TestCase):
    """A binary that is not on PATH must not escape as a raw traceback."""

    MISSING = "docker-sbx-no-such-binary-on-path"

    def setUp(self):
        self._dry = cs.DRY_RUN
        cs.DRY_RUN = False
        self.addCleanup(self._restore)

    def _restore(self):
        cs.DRY_RUN = self._dry

    def test_missing_binary_dies_with_a_message(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cs.run([self.MISSING])
        self.assertIn("command not found", err.getvalue())
        self.assertIn(self.MISSING, err.getvalue())

    def test_missing_binary_without_check_is_reported_as_a_failed_run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            proc = cs.run([self.MISSING], check=False)
        self.assertEqual(proc.returncode, 127)  # what a shell reports
        self.assertEqual(proc.stdout, "")


class SbxPreconditionTests(unittest.TestCase):
    """Without sbx nothing can work, and every sbx() call is non-fatal by design.

    Silently skipping them all and reporting success is the failure mode to avoid.
    """

    def test_main_stops_when_sbx_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.object(cs, "have", return_value=False), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                cs.main(["-n", "d", "-p", tmp])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("sbx", err.getvalue())
        self.assertIn("docker-sbx-install.py", err.getvalue())

    def test_dry_run_does_not_need_sbx(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cs, "have", return_value=False), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertEqual(cs.main(["--dry-run", "-n", "d", "-p", tmp]), 0)


class GitleaksScanTests(unittest.TestCase):
    """Telling a real finding apart from Docker not being usable.

    gitleaks exits 0 for a clean repository and non-zero when it finds something,
    so the bash original is `if ! docker run ...`. The port had drifted to
    `if not <rc>` - the opposite - which aborted on a clean repo and let a leaking
    one through.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".git").mkdir()
        self._dry = cs.DRY_RUN
        self._config = cs.CONFIG
        cs.DRY_RUN = False
        self.addCleanup(self._restore)

    def _restore(self):
        cs.DRY_RUN = self._dry
        cs.CONFIG = self._config
        self._tmp.cleanup()

    def _scan(self, daemon_rc, gitleaks_rc):
        """Run the scan with `docker info` and gitleaks faked to those exit codes."""
        cs.CONFIG = cs.parse_args(["-n", "d", "-p", str(self.root)])
        run_results = [mock.Mock(returncode=daemon_rc, stdout=""), mock.Mock(returncode=gitleaks_rc)]
        err = io.StringIO()
        code, calls = None, []
        with mock.patch.object(cs, "have", return_value=True), mock.patch.object(
            cs, "run", side_effect=run_results
        ) as fake_run, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                cs.check_project_secrets_git()
            except SystemExit as exc:
                code = exc.code
            calls = [c[0][0] for c in fake_run.call_args_list]
        return code, err.getvalue(), calls

    def test_clean_repository_continues(self):
        # The regression: this used to abort for being clean.
        code, err, _ = self._scan(daemon_rc=0, gitleaks_rc=0)
        self.assertIsNone(code)
        self.assertEqual(err, "")

    def test_leaks_found_aborts(self):
        code, err, _ = self._scan(daemon_rc=0, gitleaks_rc=1)
        self.assertEqual(code, 1)
        self.assertIn("leak", err)

    def test_daemon_down_says_so_and_is_not_a_leak_report(self):
        code, err, _ = self._scan(daemon_rc=1, gitleaks_rc=1)
        self.assertEqual(code, 1)
        self.assertIn("daemon", err)
        self.assertNotIn("leak", err)

    def test_the_daemon_is_checked_before_gitleaks_runs(self):
        _, _, calls = self._scan(daemon_rc=1, gitleaks_rc=1)
        self.assertEqual(calls, [["docker", "info"]])  # stopped before the scan

    def test_daemon_down_says_how_to_start_it(self):
        # Saying "start Docker Desktop" is not enough on its own; name the command.
        with mock.patch.object(cs.os, "name", "nt"):
            code, err, _ = self._scan(daemon_rc=1, gitleaks_rc=1)
        self.assertEqual(code, 1)
        self.assertIn("Docker Desktop", err)
        self.assertIn("Start-Process", err)


class DockerDaemonHintTests(unittest.TestCase):
    """The start instructions must match the host, not a generic guess."""

    def _lines(self, os_name, sys_platform):
        with mock.patch.object(cs.os, "name", os_name), mock.patch.object(
            cs.sys, "platform", sys_platform
        ):
            return "\n".join(cs.docker_daemon_start_lines())

    def test_windows(self):
        text = self._lines("nt", "win32")
        self.assertIn("Docker Desktop", text)
        self.assertIn("Start-Process", text)

    def test_macos(self):
        text = self._lines("posix", "darwin")
        self.assertIn("open -a Docker", text)

    def test_linux(self):
        text = self._lines("posix", "linux")
        self.assertIn("sudo systemctl start docker", text)
        self.assertIn("sudo service docker start", text)  # the no-systemd fallback


class ProfileParserTests(unittest.TestCase):
    # The values the shipped profiles must produce, spelled out so a change in the
    # parser (or a profile) is visible rather than silently absorbed.
    EXPECTED = {
        "python.yaml": ["pypi.org", "files.pythonhosted.org", "test.pypi.org"],
        "node.yaml": [
            "registry.npmjs.org", "npmjs.org", "yarnpkg.com",
            "registry.yarnpkg.com", "pnpm.io",
        ],
        "rust.yaml": ["crates.io", "static.crates.io"],
        "dotnet.yaml": ["nuget.org", "api.nuget.org"],
        "flutter.yaml": ["pub.dev", "storage.googleapis.com"],
    }

    def _load(self, name):
        path = REPO / "profiles" / name
        return cs.validate_profile(cs.parse_profile_minimal(path.read_text(), str(path)), str(path))

    def test_every_shipped_profile_parses(self):
        profiles = sorted((REPO / "profiles").glob("*.y*ml"))
        self.assertTrue(profiles, "no profiles found")
        for path in profiles:
            with self.subTest(profile=path.name):
                allowed, denied = cs.validate_profile(
                    cs.parse_profile_minimal(path.read_text(), str(path)), str(path)
                )
                self.assertTrue(allowed, f"{path.name} produced no allow rules")
                self.assertEqual(denied, [])

    def test_expected_allow_lists(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(profile=name):
                allowed, _ = self._load(name)
                self.assertEqual(allowed, expected)

    def test_a_profile_with_deny_and_comments(self):
        text = (
            "# a profile\n"
            "name: custom\n"
            "description: with a comment  # trailing\n"
            "policies:\n"
            "  network:\n"
            "    allow:\n"
            "      - good.example.com\n"
            "    deny:\n"
            '      - "bad.example.com"\n'
        )
        allowed, denied = cs.validate_profile(cs.parse_profile_minimal(text, "t"), "t")
        self.assertEqual(allowed, ["good.example.com"])
        self.assertEqual(denied, ["bad.example.com"])

    def test_empty_document_is_an_empty_mapping(self):
        self.assertEqual(cs.parse_profile_minimal("", "t"), {})

    def test_rejections(self):
        cases = {
            "forbidden global key": "global:\n  x: 1\n",
            "non-network section": "policies:\n  filesystem:\n    read:\n      - /x\n",
            "allow is a string": "policies:\n  network:\n    allow: pypi.org\n",
            "allow is a mapping": "policies:\n  network:\n    allow:\n      x: y\n",
            "blank entry": 'policies:\n  network:\n    allow:\n      - ""\n',
            "anchor": "name: &a x\n",
            "flow list": "policies:\n  network:\n    allow: [a, b]\n",
            "block scalar": "description: |\n  multi\n",
            "tab indent": "policies:\n\tnetwork:\n",
            "not a mapping": "- just\n- a list\n",
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(cs.ProfileError):
                    data = cs.parse_profile_minimal(text, "t")
                    cs.validate_profile(data, "t")

    def test_parser_errors_are_prefixed_with_the_profile(self):
        # load_profile is what the CLI calls; it must name the offending file.
        try:
            import yaml  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("PyYAML is installed, so the minimal parser is not used")
        path = Path(tempfile.mkdtemp()) / "p.yaml"
        path.write_text("name: &anchor x\n")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.load_profile(str(path))
        self.assertIn(str(path), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
