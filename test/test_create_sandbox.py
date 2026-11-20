#!/usr/bin/env python3
"""Unit tests for docker-sbx-create-sandbox.py (standard library only).

Covers the pure helpers the CLI relies on: argument parsing, the env-file reader,
volume specs, Slack and Nextcloud Talk validation, provider-script staging, host
extraction, the secrets-file scanner, the minimal YAML profile parser, the
sandbox-name pre-check and the wait for a new sandbox's first start. It also
covers the newer per-sandbox workflow: mode selection (create/update/recreate/
from-config), profile bundles and their asset plans, the config file renderer and
reader, and the main() branches that drive them. The end-to-end behaviour is
covered separately by test/run-tests.sh, which drives this implementation's CLI.

    python3 -m unittest discover -s test -p 'test_*.py'
    (or, from inside test/:  python3 -m unittest test_create_sandbox)
"""

import contextlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath
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

    def test_nctalk_enabled_is_exact(self):
        self.envfile.write_text("SBX_NCTALK_ENABLED=true\n")
        self.assertTrue(cs.nctalk_enabled())
        self.envfile.write_text("SBX_NCTALK_ENABLED=True\n")
        self.assertFalse(cs.nctalk_enabled())

    def test_nctalk_events_default(self):
        self.envfile.write_text("X=1\n")
        self.assertEqual(cs.nctalk_events(), cs.DEFAULT_NCTALK_EVENTS)


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


class NCTalkValidationTests(unittest.TestCase):
    """Nextcloud Talk is the Slack feature's second provider, switched on by
    SBX_NCTALK_ENABLED; its target is a full URL/user/app-password/room set."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.envfile = Path(self._tmp.name) / "sbx.env"
        cs.CONFIG.envfile = str(self.envfile)
        cs.CONFIG.name = "probe"

    def tearDown(self):
        self._tmp.cleanup()
        cs.CONFIG.envfile = ""
        cs.CONFIG.name = ""

    COMPLETE = {
        "SBX_NCTALK_URL": "https://cloud.example.com",
        "SBX_NCTALK_USER": "agent",
        "SBX_NCTALK_TOKEN": "app-pw",
        "SBX_NCTALK_ROOM": "abc123",
    }

    def _run(self, body):
        self.envfile.write_text(body)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cs.validate_nctalk_config()

    def test_disabled_is_a_no_op(self):
        self._run("SBX_NCTALK_ENABLED=false\n")

    def test_complete_target_is_accepted(self):
        self._run("SBX_NCTALK_ENABLED=true\n" + "".join(f"{k}={v}\n" for k, v in self.COMPLETE.items()))

    def test_http_url_with_a_port_is_accepted(self):
        target = dict(self.COMPLETE, SBX_NCTALK_URL="http://host.docker.internal:8080")
        self._run("SBX_NCTALK_ENABLED=true\n" + "".join(f"{k}={v}\n" for k, v in target.items()))

    def test_url_requires_an_http_scheme_and_a_valid_host(self):
        for url in (
            "cloud.example.com",
            "ftp://cloud.example.com",
            "https:///missing-host",
            "https://cloud.example.com:not-a-port",
            "https://cloud.example.com/a path",
        ):
            with self.subTest(url=url):
                body = "SBX_NCTALK_ENABLED=true\n" + "".join(
                    f"{k}={url if k == 'SBX_NCTALK_URL' else v}\n"
                    for k, v in self.COMPLETE.items()
                )
                self.envfile.write_text(body)
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    err
                ), self.assertRaises(SystemExit):
                    cs.validate_nctalk_config()
                self.assertIn("SBX_NCTALK_URL must be an absolute http:// or https:// URL", err.getvalue())

    def test_each_missing_field_aborts(self):
        for dropped in self.COMPLETE:
            with self.subTest(missing=dropped):
                body = "SBX_NCTALK_ENABLED=true\n" + "".join(
                    f"{k}={v}\n" for k, v in self.COMPLETE.items() if k != dropped
                )
                with self.assertRaises(SystemExit):
                    self._run(body)

    def test_the_error_names_the_missing_variable(self):
        err = io.StringIO()
        body = "SBX_NCTALK_ENABLED=true\nSBX_NCTALK_URL=https://cloud.example.com\n"
        self.envfile.write_text(body)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit):
            cs.validate_nctalk_config()
        self.assertIn("SBX_NCTALK_USER", err.getvalue())

    def test_unknown_event_aborts(self):
        body = (
            "SBX_NCTALK_ENABLED=true\n"
            + "".join(f"{k}={v}\n" for k, v in self.COMPLETE.items())
            + "SBX_NCTALK_EVENTS=Stop,Bogus\n"
        )
        with self.assertRaises(SystemExit):
            self._run(body)

    def test_all_supported_events_accepted(self):
        body = (
            "SBX_NCTALK_ENABLED=true\n"
            + "".join(f"{k}={v}\n" for k, v in self.COMPLETE.items())
            + "SBX_NCTALK_EVENTS=" + ",".join(cs.SUPPORTED_NCTALK_EVENTS) + "\n"
        )
        self._run(body)


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


class NCTalkStagingTests(unittest.TestCase):
    """The provider scripts are staged independently, so a sandbox can run Slack
    only, Talk only, or both."""

    SLACK = "SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n"
    TALK = (
        "SBX_NCTALK_ENABLED=true\nSBX_NCTALK_URL=https://cloud.example.com\n"
        "SBX_NCTALK_USER=a\nSBX_NCTALK_TOKEN=t\nSBX_NCTALK_ROOM=r\n"
    )

    def setUp(self):
        self._config = cs.CONFIG
        self._dry = cs.DRY_RUN
        cs.DRY_RUN = False
        self.addCleanup(self._restore)

    def _restore(self):
        cs.CONFIG = self._config
        cs.DRY_RUN = self._dry

    def _stage(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = Path(tmp.name) / "sbx.env"
        env.write_text(body)
        cs.CONFIG = cs.parse_args(["-n", "d", "-p", tmp.name, "-e", str(env)])
        with mock.patch.object(cs, "sbx") as fake_sbx, mock.patch.object(
            cs, "wait_for_sandbox", return_value=True
        ), contextlib.redirect_stdout(io.StringIO()):
            cs.setup_sandbox()
        return " ".join(" ".join(call[0][0]) for call in fake_sbx.call_args_list)

    def test_talk_script_is_staged_only_when_enabled(self):
        self.assertIn("nctalk-notify.sh", self._stage(self.TALK))
        self.assertNotIn("nctalk-notify.sh", self._stage("SBX_NCTALK_ENABLED=false\n"))

    def test_both_providers_can_be_staged_together(self):
        calls = self._stage(self.SLACK + self.TALK)
        self.assertIn("slack-notify.sh", calls)
        self.assertIn("nctalk-notify.sh", calls)


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

    def test_setup_waits_for_readiness_before_any_configuration_command(self):
        # The readiness probe is an `sbx exec`, which Docker documents as starting
        # a stopped sandbox. Setup must wait for that gate before its first cp/exec
        # configuration command, rather than issuing a separate or interactive run.
        with tempfile.TemporaryDirectory() as tmp:
            cs.CONFIG = cs.parse_args(["-n", "d", "-p", tmp])
            with mock.patch.object(cs, "sbx") as fake_sbx, mock.patch.object(
                cs, "wait_for_sandbox", return_value=True
            ) as wait:
                ordered = mock.Mock()
                ordered.attach_mock(wait, "wait")
                ordered.attach_mock(fake_sbx, "sbx")
                cs.setup_sandbox()

        calls = [call[0] for call in ordered.mock_calls]
        self.assertEqual(calls[0], "wait")
        self.assertIn("sbx", calls)

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


class ModeSelectionTests(unittest.TestCase):
    """The create CLI has three modes: create (default), recreate, from-config.

    Applying profiles to a live sandbox is its own command
    (docker-sbx-apply-profile.py); it reuses the same flag validator under the
    "update" mode, which is covered here and in test_apply_profile.py.
    """

    def test_new_boolean_flags_parse(self):
        for flag, attribute in (
            ("--recreate", "recreate"),
            ("--force", "force"),
            ("--no-config", "no_config"),
        ):
            with self.subTest(flag=flag):
                cfg = cs.parse_args([flag])
                self.assertTrue(getattr(cfg, attribute))
                self.assertEqual(cfg.seen, {attribute})

    def test_new_value_flags_parse(self):
        cfg = cs.parse_args(["--from-config", "s.yaml", "--config-dir", "/cfg"])
        self.assertEqual(cfg.from_config, "s.yaml")
        self.assertEqual(cfg.config_dir, "/cfg")
        self.assertEqual(cfg.seen, {"from_config", "config_dir"})

    def test_seen_records_every_named_option(self):
        cfg = cs.parse_args(["-n", "d", "-p", "/x", "-v", "/a", "-v", "/b"])
        self.assertEqual(cfg.seen, {"name", "path", "volumes"})

    def test_select_mode(self):
        cases = {
            ("-n", "d", "-p", "/x"): "create",
            ("-n", "d", "--recreate"): "recreate",
            ("--from-config", "s.yaml"): "from-config",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(cs.select_mode(cs.parse_args(list(argv))), expected)

    def test_two_modes_name_both_flags(self):
        for argv in (
            ("--recreate", "--from-config", "s.yaml"),
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(cs.CliError) as caught:
                    cs.select_mode(cs.parse_args(list(argv)))
                message = str(caught.exception)
                self.assertIn("Only one of", message)
                for flag in (item for item in argv if item.startswith("-")):
                    self.assertIn(flag, message)

    def test_force_requires_recreate(self):
        for argv in (("--force",), ("--force", "--from-config", "s.yaml")):
            with self.subTest(argv=argv):
                cfg = cs.parse_args(list(argv))
                with self.assertRaises(cs.CliError) as caught:
                    cs.validate_mode_flags(cfg, cs.select_mode(cfg))
                self.assertIn("--force is only valid with --recreate", str(caught.exception))

    def test_create_accepts_the_historical_flag_set(self):
        cfg = cs.parse_args(
            ["-n", "d", "-p", "/x", "-e", "f.env", "-s", "false", "-sf", "s.json",
             "-gsf", "g.json", "-v", "/a", "-profile", "python",
             "-profile-directory", "/p", "--no-config", "--dry-run"]
        )
        self.assertEqual(cs.select_mode(cfg), "create")
        cs.validate_mode_flags(cfg, "create")  # must not raise

    def test_update_rejects_create_only_flags(self):
        cases = {
            "path": ("-p", "/x", "-p"),
            "envfile": ("-e", "f.env", "-e"),
            "secrets": ("-s", "false", "-s"),
            "secretsfile": ("-sf", "s.json", "-sf"),
            "volumes": ("-v", "/a", "-v"),
        }
        for label, (option, value, shown) in cases.items():
            with self.subTest(option=option):
                cfg = cs.parse_args(["-n", "d", "-profile", "python", option, value])
                with self.assertRaises(cs.CliError) as caught:
                    cs.validate_mode_flags(cfg, "update")
                self.assertIn(
                    f"Option {shown} cannot be used with the apply-profile command",
                    str(caught.exception),
                )

    def test_recreate_rejects_create_only_flags(self):
        cases = {
            "path": ("-p", "/x", "-p"),
            "volumes": ("-v", "/a", "-v"),
            "profiles": ("-profile", "python", "-profile"),
        }
        for label, (option, value, shown) in cases.items():
            with self.subTest(option=option):
                cfg = cs.parse_args(["-n", "d", "--recreate", option, value])
                self.assertEqual(cs.select_mode(cfg), "recreate")
                with self.assertRaises(cs.CliError) as caught:
                    cs.validate_mode_flags(cfg, "recreate")
                self.assertIn(
                    f"Option {shown} cannot be used with --recreate",
                    str(caught.exception),
                )

    def test_from_config_rejects_create_only_flags(self):
        cases = {
            "name": ("-n", "d", "-n"),
            "path": ("-p", "/x", "-p"),
            "profiles": ("-profile", "python", "-profile"),
        }
        for label, (option, value, shown) in cases.items():
            with self.subTest(option=option):
                cfg = cs.parse_args(["--from-config", "s.yaml", option, value])
                self.assertEqual(cs.select_mode(cfg), "from-config")
                with self.assertRaises(cs.CliError) as caught:
                    cs.validate_mode_flags(cfg, "from-config")
                self.assertIn(
                    f"Option {shown} cannot be used with --from-config",
                    str(caught.exception),
                )

    def test_help_returns_immediately_and_documents_the_new_modes(self):
        # -h is handled before the rest of the argv, so a bad trailing option is
        # not even looked at.
        self.assertTrue(cs.parse_args(["-h", "--bogus"]).show_help)
        self.assertTrue(cs.parse_args(["--help", "--bogus"]).show_help)
        text = cs.usage_text()
        for needle in ("--recreate", "--from-config", "--config-dir",
                       "--no-config", "--force"):
            self.assertIn(needle, text)
        self.assertNotIn("--apply-profile", text)
        self.assertIn("docker-sbx-apply-profile.py", text)

    def test_update_help_documents_only_the_update_flags(self):
        text = cs.usage_text("update")
        for needle in ("-profile", "-profile-directory", "--config-dir",
                       "--no-config", "--dry-run"):
            self.assertIn(needle, text)
        for absent in ("-p [project path]", "--recreate", "--from-config", "--force"):
            self.assertNotIn(absent, text)


class ResolveProfileTests(unittest.TestCase):
    """A profile is either <dir>/<name>/profile.yaml or <dir>/<name>.yaml.

    Defining the same name twice is ambiguous, so it is refused instead of
    resolved by precedence.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, relative, text=""):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def test_directory_form(self):
        definition = self._write("demo/profile.yaml")
        source = cs.resolve_profile(self.root, "demo")
        self.assertEqual(source.name, "demo")
        self.assertEqual(source.definition, definition)
        self.assertEqual(source.assets_root, self.root / "demo")

    def test_directory_form_accepts_profile_yml(self):
        definition = self._write("demo/profile.yml")
        source = cs.resolve_profile(self.root, "demo")
        self.assertEqual(source.definition, definition)
        self.assertEqual(source.assets_root, self.root / "demo")

    def test_flat_forms(self):
        for extension in ("yaml", "yml"):
            with self.subTest(extension=extension):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                root = Path(tmp.name)
                definition = root / f"demo.{extension}"
                definition.write_text("")
                source = cs.resolve_profile(root, "demo")
                self.assertEqual(source.definition, definition)
                self.assertIsNone(source.assets_root)

    def test_directory_and_flat_together_is_an_error(self):
        self._write("demo/profile.yaml")
        self._write("demo.yaml")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.resolve_profile(self.root, "demo")
        self.assertIn("defined both as a directory", str(caught.exception))

    def test_profile_yaml_and_profile_yml_together_is_an_error(self):
        self._write("demo/profile.yaml")
        self._write("demo/profile.yml")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.resolve_profile(self.root, "demo")
        self.assertIn("both profile.yaml and profile.yml", str(caught.exception))

    def test_flat_yaml_and_yml_together_is_an_error(self):
        self._write("demo.yaml")
        self._write("demo.yml")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.resolve_profile(self.root, "demo")
        self.assertIn("exists as both demo.yaml and demo.yml", str(caught.exception))

    def test_missing_profile_names_the_directory(self):
        with self.assertRaises(cs.ProfileError) as caught:
            cs.resolve_profile(self.root, "nope")
        self.assertEqual(
            str(caught.exception),
            f"profile 'nope' not found in '{self.root}'",
        )


class ProfileAssetPlanTests(unittest.TestCase):
    """A profile directory may carry files that mirror the sandbox's ~/.claude.

    The plan decides what is installed at agent level and what is installed at
    project level, and refuses anything that could escape either tree.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _touch(self, root, relative, data=b"x", mode=None):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if mode is not None:
            os.chmod(target, mode)
        return target

    def test_plan_splits_agent_and_project_levels(self):
        tree = [
            ("profile.yaml", b"name: demo\n"),
            ("CLAUDE.md", b"root file"),
            ("skills/x/SKILL.md", b"skill"),
            ("agent/agents/h.md", b"helper"),
            ("project/commands/c.md", b"command"),
        ]
        for relative, data in tree:
            self._touch(self.root, relative, data)

        agent, project = cs.profile_asset_plan(self.root)

        self.assertEqual([entry[0] for entry in agent],
                         ["CLAUDE.md", "agents/h.md", "skills/x/SKILL.md"])
        self.assertEqual([entry[0] for entry in project], ["commands/c.md"])
        for arcname, source, mode, size in agent + project:
            self.assertIsInstance(source, Path)
            self.assertTrue(source.is_file())
            self.assertEqual(size, source.stat().st_size)
            self.assertIsInstance(mode, int)

    def test_profile_definition_is_never_installed(self):
        self._touch(self.root, "profile.yaml", b"name: demo\n")
        self._touch(self.root, "profile.yml", b"name: demo\n")
        self._touch(self.root, "CLAUDE.md", b"hi")
        agent, project = cs.profile_asset_plan(self.root)
        self.assertEqual([entry[0] for entry in agent + project], ["CLAUDE.md"])

    def test_plan_is_deterministic(self):
        for relative in ("skills/x/SKILL.md", "skills/a.md", "agent/agents/h.md",
                         "CLAUDE.md", "README.md", "project/c.md"):
            self._touch(self.root, relative)
        first = cs.profile_asset_plan(self.root)
        second = cs.profile_asset_plan(self.root)
        self.assertEqual(first, second)
        self.assertEqual(
            [entry[0] for entry in first[0]],
            ["CLAUDE.md", "README.md", "agents/h.md", "skills/a.md",
             "skills/x/SKILL.md"],
        )

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_are_refused(self):
        self._touch(self.root, "CLAUDE.md", b"hi")
        os.symlink(self.root / "CLAUDE.md", self.root / "link")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.profile_asset_plan(self.root)
        self.assertIn("symlink", str(caught.exception))
        self.assertIn("link", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_inside_a_subdirectory_are_refused(self):
        self._touch(self.root, "skills/x", b"x")
        self._touch(self.root, "other/y", b"y")
        os.symlink(self.root / "other", self.root / "skills" / "linked")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.profile_asset_plan(self.root)
        self.assertIn("symlink", str(caught.exception))

    def test_managed_agent_files_are_refused(self):
        for name in ("settings.json", "settings.local.json", ".credentials.json"):
            with self.subTest(name=name):
                root = self._root()
                self._touch(root, name, b"{}")
                with self.assertRaises(cs.ProfileError) as caught:
                    cs.profile_asset_plan(root)
                message = str(caught.exception)
                self.assertIn(name, message)
                self.assertIn("agent level", message)

    def test_managed_project_paths_are_refused(self):
        for relative in (".git/config", ".credentials.json"):
            with self.subTest(relative=relative):
                root = self._root()
                self._touch(root, f"project/{relative}", b"x")
                with self.assertRaises(cs.ProfileError) as caught:
                    cs.profile_asset_plan(root)
                message = str(caught.exception)
                self.assertIn(relative.split("/")[0], message)
                self.assertIn("project level", message)

    def test_agent_and_project_must_be_directories(self):
        for name in ("agent", "project"):
            with self.subTest(name=name):
                root = self._root()
                self._touch(root, name, b"not a directory")
                with self.assertRaises(cs.ProfileError) as caught:
                    cs.profile_asset_plan(root)
                self.assertIn(f"'{name}' must be a directory", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "os.mkfifo is not available on Windows")
    def test_non_regular_files_are_refused(self):
        root = self._root()
        (root / "skills").mkdir()
        try:
            os.mkfifo(root / "skills" / "pipe")
        except (AttributeError, OSError) as exc:
            self.skipTest(f"cannot create a FIFO here: {exc}")
        with self.assertRaises(cs.ProfileError) as caught:
            cs.profile_asset_plan(root)
        self.assertIn("not a regular file", str(caught.exception))
        self.assertIn("pipe", str(caught.exception))

    def test_the_file_count_limit_is_enforced(self):
        self._touch(self.root, "a", b"a")
        self._touch(self.root, "b", b"b")
        with mock.patch.object(cs, "ASSET_MAX_FILES", 1):
            with self.assertRaises(cs.ProfileError) as caught:
                cs.profile_asset_plan(self.root)
        self.assertIn("too large to install", str(caught.exception))
        self.assertIn("2 files", str(caught.exception))

    def test_the_byte_limit_is_enforced(self):
        self._touch(self.root, "a", b"ab")
        with mock.patch.object(cs, "ASSET_MAX_BYTES", 1):
            with self.assertRaises(cs.ProfileError) as caught:
                cs.profile_asset_plan(self.root)
        self.assertIn("too large to install", str(caught.exception))


class AssetTarballTests(unittest.TestCase):
    """The bundle is built in-process (no host tar) and reproducibly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, relative, data, mode):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, mode)
        return target

    def test_archive_members_are_normalised(self):
        self._write("CLAUDE.md", b"hi\n", 0o644)
        self._write("skills/x/SKILL.md", b"#!/bin/sh\n", 0o755)
        agent, project = cs.profile_asset_plan(self.root)
        files = agent + project

        destination = self.root / "bundle.tgz"
        cs.build_asset_tarball(files, destination)
        self.assertTrue(destination.is_file())

        with tarfile.open(destination) as archive:
            members = archive.getmembers()
            self.assertEqual([member.name for member in members],
                             [entry[0] for entry in files])
            for member in members:
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.uname, "")
                self.assertEqual(member.gname, "")
                self.assertEqual(member.mtime, 0)
                self.assertFalse(member.name.startswith("/"))
                self.assertNotIn("..", PurePosixPath(member.name).parts)
            by_name = {member.name: member for member in members}
            self.assertEqual(by_name["CLAUDE.md"].mode, 0o644)
            self.assertEqual(by_name["skills/x/SKILL.md"].mode, 0o755)


class SandboxProjectPathTests(unittest.TestCase):
    def test_windows_paths_are_mapped_into_the_sandbox(self):
        cases = {
            r"C:\Users\me\proj": "/c/Users/me/proj",
            "C:/x/y": "/c/x/y",
            r"D:\a": "/d/a",
            "/mnt/c/x": "/mnt/c/x",
            "relative/path": "relative/path",
        }
        for host_path, expected in cases.items():
            with self.subTest(host_path=host_path):
                self.assertEqual(cs.sandbox_project_path(host_path), expected)


class DefaultConfigDirTests(unittest.TestCase):
    """The saved-config directory follows each platform's convention."""

    def test_linux_uses_xdg_config_home_when_set(self):
        with mock.patch.object(cs.os, "name", "posix"), mock.patch.object(
            cs.sys, "platform", "linux"
        ), mock.patch.dict(
            cs.os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}, clear=False
        ), mock.patch.object(
            cs, "Path", PurePosixPath
        ):
            self.assertEqual(
                cs.default_config_dir(),
                PurePosixPath("/tmp/xdg") / "docker-sbx" / "sandboxes",
            )

    @unittest.skipIf(os.name == "nt", "Path.home() would select WindowsPath")
    def test_linux_defaults_to_dot_config_under_home(self):
        env = {key: value for key, value in cs.os.environ.items()
               if key != "XDG_CONFIG_HOME"}
        with mock.patch.object(cs.os, "name", "posix"), mock.patch.object(
            cs.sys, "platform", "linux"
        ), mock.patch.dict(cs.os.environ, env, clear=True):
            result = cs.default_config_dir()
        self.assertEqual(result, Path.home() / ".config" / "docker-sbx" / "sandboxes")
        self.assertTrue(str(result).endswith(".config/docker-sbx/sandboxes"))

    @unittest.skipIf(os.name == "nt", "Path.home() would select WindowsPath")
    def test_macos_uses_application_support(self):
        with mock.patch.object(cs.os, "name", "posix"), mock.patch.object(
            cs.sys, "platform", "darwin"
        ):
            result = cs.default_config_dir()
        self.assertEqual(
            result,
            Path.home() / "Library" / "Application Support" / "docker-sbx" / "sandboxes",
        )
        self.assertIn("Library/Application Support/docker-sbx/sandboxes", result.as_posix())

    def test_windows_uses_appdata(self):
        appdata = "/appdata/roaming"
        # cs.Path is pinned to a pure class so this branch can be exercised on a
        # POSIX host too; APPDATA is set, so Path.home() is never reached.
        with mock.patch.object(cs.os, "name", "nt"), mock.patch.dict(
            cs.os.environ, {"APPDATA": appdata}, clear=False
        ), mock.patch.object(cs, "Path", PurePosixPath):
            result = cs.default_config_dir()
        self.assertEqual(
            result, PurePosixPath(appdata) / "docker-sbx" / "sandboxes"
        )


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._config = cs.CONFIG
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)
        self.root = Path(self._tmp.name)
        cs.CONFIG = cs.parse_args(["-n", "demo", "-p", "/x"])
        cs.CONFIG.config_dir = str(self.root / "cfg")

    def _restore(self):
        cs.CONFIG = self._config

    def test_names_that_would_escape_the_directory_are_refused(self):
        for name in ("", ".", "..", "a/b", "a\\b"):
            with self.subTest(name=name):
                with self.assertRaises(cs.ConfigError) as caught:
                    cs.config_file(name)
                self.assertIn("cannot be used as a config file name", str(caught.exception))

    def test_path_is_the_config_dir_plus_the_sandbox_name(self):
        self.assertEqual(cs.config_file("demo"), self.root / "cfg" / "demo.yaml")


class ConfigRoundTripTests(unittest.TestCase):
    """render_config and load_config must agree, including awkward paths."""

    PROJECT = "/home/o'brien/my proj"
    VOLUME = "/home/o'brien/data dir:rw"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _values(self, **overrides):
        values = {
            "name": "demo",
            "project": self.PROJECT,
            "env_file": "/home/o'brien/env file.env",
            "scan_secrets": "false",
            "secrets_settings": "/home/o'brien/scan it.json",
            "profiles": ["python", "node"],
            "profile_directory": "/home/o'brien/profiles dir",
            "volumes": [self.VOLUME],
        }
        values.update(overrides)
        return values

    def _base(self, **overrides):
        data = {"version": 1, "name": "demo", "project": "/p"}
        data.update(overrides)
        return data

    def test_all_keys_round_trip(self):
        values = self._values()
        target = self.root / "demo.yaml"
        target.write_text(cs.render_config(values))

        loaded = cs.load_config(str(target))

        self.assertEqual(loaded["name"], "demo")
        self.assertEqual(loaded["project"], self.PROJECT)
        self.assertEqual(loaded["env_file"], values["env_file"])
        self.assertEqual(loaded["scan_secrets"], "false")
        self.assertEqual(loaded["secrets_settings"], values["secrets_settings"])
        self.assertEqual(loaded["profiles"], ["python", "node"])
        self.assertEqual(loaded["profile_directory"], values["profile_directory"])
        self.assertEqual(loaded["volumes"], [self.VOLUME])

    def test_minimal_parser_unescapes_single_quotes(self):
        # The writer escapes an apostrophe as '' and _scalar must map it back.
        text = cs.render_config(self._values())
        parsed = cs.parse_profile_minimal(text, "config")
        self.assertEqual(parsed["project"], self.PROJECT)
        self.assertEqual(parsed["env_file"], "/home/o'brien/env file.env")
        self.assertEqual(parsed["volumes"], [self.VOLUME])

    def test_optional_keys_are_omitted_when_empty(self):
        text = cs.render_config({"name": "demo", "project": "/p",
                                 "scan_secrets": "true"})
        for key in ("env_file:", "secrets_settings:", "profile_directory:",
                    "profiles:", "volumes:"):
            self.assertNotIn(key, text)
        self.assertIn("scan_secrets: true", text)

    def test_scan_secrets_accepts_bools_and_strings(self):
        for value, expected in (
            (True, "true"), (False, "false"),
            ("true", "true"), ("false", "false"),
            ("TRUE", "true"), ("False", "false"),
        ):
            with self.subTest(value=value):
                loaded = cs.validate_config(
                    self._base(scan_secrets=value), "config x"
                )
                self.assertEqual(loaded["scan_secrets"], expected)

    def test_scan_secrets_rejects_anything_else(self):
        with self.assertRaises(cs.ConfigError) as caught:
            cs.validate_config(self._base(scan_secrets="yes"), "config x")
        self.assertIn("'scan_secrets' must be true or false", str(caught.exception))

    def test_version_must_be_the_supported_one(self):
        for data in (
            {"name": "d", "project": "/p"},
            self._base(version=2),
            self._base(version="2"),
        ):
            with self.subTest(version=data.get("version")):
                with self.assertRaises(cs.ConfigError) as caught:
                    cs.validate_config(data, "config x")
                self.assertIn("unsupported version", str(caught.exception))

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(cs.ConfigError) as caught:
            cs.validate_config(self._base(bogus=1), "config x")
        self.assertIn("unknown key 'bogus'", str(caught.exception))

    def test_name_and_project_are_required(self):
        cases = {
            "missing": {k: v for k, v in self._base().items() if k != "project"},
            "empty": self._base(name=""),
            "relative": self._base(project="relative/path"),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(cs.ConfigError):
                    cs.validate_config(data, "config x")

    def test_relative_optional_paths_are_refused(self):
        for key in ("env_file", "secrets_settings", "profile_directory"):
            with self.subTest(key=key):
                with self.assertRaises(cs.ConfigError) as caught:
                    cs.validate_config(self._base(**{key: "relative"}), "config x")
                self.assertIn(key, str(caught.exception))
                self.assertIn("must be an absolute path", str(caught.exception))

    def test_lists_must_hold_non_empty_strings(self):
        cases = {
            "volumes not a list": {"volumes": "x"},
            "profiles not a list": {"profiles": {"a": 1}},
            "blank entry": {"volumes": [""]},
            "non-string entry": {"profiles": [1]},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(cs.ConfigError):
                    cs.validate_config(self._base(**overrides), "config x")

    def test_load_config_reports_a_missing_file(self):
        with self.assertRaises(cs.ConfigError) as caught:
            cs.load_config(str(self.root / "absent.yaml"))
        self.assertIn("failed to read", str(caught.exception))

    def test_load_config_rejects_a_non_mapping_document(self):
        target = self.root / "list.yaml"
        target.write_text("- a\n- b\n")
        with self.assertRaises(cs.ConfigError) as caught:
            cs.load_config(str(target))
        self.assertIn("must be a YAML mapping", str(caught.exception))


class WriteConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_creates_parent_directories_and_writes_the_text(self):
        target = self.root / "a" / "b" / "demo.yaml"
        cs.write_config_file(target, "version: 1\n")
        self.assertEqual(target.read_text(), "version: 1\n")
        self.assertEqual([entry.name for entry in target.parent.iterdir()],
                         ["demo.yaml"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_file_is_private(self):
        target = self.root / "demo.yaml"
        cs.write_config_file(target, "version: 1\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class SaveCreatedConfigTests(unittest.TestCase):
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

    def _configure(self, *extra):
        cs.CONFIG = cs.parse_args(
            ["-n", "demo", "-p", str(self.project),
             "--config-dir", str(self.config_dir), *extra]
        )

    def test_dry_run_prints_the_config_and_writes_nothing(self):
        self._configure()
        cs.DRY_RUN = True
        target = self.config_dir / "demo.yaml"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cs.save_created_config()
        self.assertEqual(result, "")
        self.assertIn(f"Config would be saved: {target}", out.getvalue())
        self.assertIn("version: 1", out.getvalue())
        self.assertIn("name: 'demo'", out.getvalue())
        self.assertFalse(target.exists())

    def test_a_real_run_writes_the_file_and_returns_its_path(self):
        self._configure()
        cs.DRY_RUN = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cs.save_created_config()
        target = self.config_dir / "demo.yaml"
        self.assertEqual(result, str(target))
        self.assertEqual(out.getvalue(), "")
        self.assertIn(f"project: '{self.project}'", target.read_text())

    def test_no_config_writes_nothing(self):
        self._configure("--no-config")
        cs.DRY_RUN = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cs.save_created_config()
        self.assertEqual(result, "")
        self.assertFalse((self.config_dir / "demo.yaml").exists())
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_an_unwritable_directory_warns_instead_of_raising(self):
        blocker = self.root / "blocker"
        blocker.write_text("not a directory")
        self._configure()
        cs.CONFIG.config_dir = str(blocker / "nested")
        cs.DRY_RUN = False
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            result = cs.save_created_config()
        self.assertEqual(result, "")
        self.assertIn("Warning:", err.getvalue())
        self.assertIn("failed to write", err.getvalue())


class AddProfilesToConfigTextTests(unittest.TestCase):
    """Recording a profile must not reformat or reorder the rest of the file."""

    def test_appends_missing_names_after_the_existing_list(self):
        text = (
            "version: 1\n"
            "name: 'd'\n"
            "project: '/p'\n"
            "profiles:\n"
            "  - 'python'\n"
            "\n"
            "# mounts\n"
            "volumes:\n"
            "  - '/v'\n"
        )
        updated = cs.add_profiles_to_config_text(text, ["node", "python", "rust"])
        self.assertEqual(
            updated,
            "version: 1\n"
            "name: 'd'\n"
            "project: '/p'\n"
            "profiles:\n"
            "  - 'python'\n"
            "  - 'node'\n"
            "  - 'rust'\n"
            "\n"
            "# mounts\n"
            "volumes:\n"
            "  - '/v'\n",
        )

    def test_existing_names_are_not_duplicated(self):
        text = "version: 1\nprofiles:\n  - 'python'\n  - 'node'\n"
        self.assertEqual(
            cs.add_profiles_to_config_text(text, ["python", "node"]), text
        )

    def test_a_profiles_block_is_inserted_when_absent(self):
        text = "version: 1\nname: 'd'\nproject: '/p'\n"
        self.assertEqual(
            cs.add_profiles_to_config_text(text, ["python"]),
            text + "\nprofiles:\n  - 'python'\n",
        )

    def test_a_profiles_block_with_no_items_gets_the_names(self):
        text = "version: 1\nprofiles:\nvolumes:\n  - '/v'\n"
        self.assertEqual(
            cs.add_profiles_to_config_text(text, ["python"]),
            "version: 1\nprofiles:\n  - 'python'\nvolumes:\n  - '/v'\n",
        )

    def test_an_inline_flow_list_is_rewritten_as_a_block(self):
        # The dependency-free parser cannot read a flow list, so recording a new
        # profile must leave the file in the block form it can read.
        self.assertEqual(
            cs.add_profiles_to_config_text("profiles: [python, node]\n", ["rust"]),
            "profiles:\n  - 'python'\n  - 'node'\n  - 'rust'\n",
        )

    def test_duplicate_names_are_recorded_once(self):
        self.assertEqual(
            cs.add_profiles_to_config_text("profiles:\n  - 'python'\n", ["node", "node"]),
            "profiles:\n  - 'python'\n  - 'node'\n",
        )


class UpdateSavedConfigTests(unittest.TestCase):
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
        self.target = self.config_dir / "demo.yaml"
        self._configure()

    def _restore(self):
        cs.CONFIG = self._config
        cs.DRY_RUN = self._dry

    def _configure(self, *extra):
        cs.CONFIG = cs.parse_args(
            ["-n", "demo", "-p", str(self.project),
             "--config-dir", str(self.config_dir), *extra]
        )

    def _write_existing(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.target.write_text(
            cs.render_config({"name": "demo", "project": str(self.project),
                              "scan_secrets": "true", "profiles": ["python"]})
        )

    def test_an_existing_config_gets_the_profile_and_is_reported(self):
        self._write_existing()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cs.update_saved_config(["node"])
        self.assertIn(f"Config updated: {self.target}", out.getvalue())
        self.assertEqual(cs.load_config(str(self.target))["profiles"],
                         ["python", "node"])

    def test_a_missing_config_warns_and_writes_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cs.update_saved_config(["node"])
        self.assertIn("no saved config for sandbox 'demo'", err.getvalue())
        self.assertFalse(self.target.exists())

    def test_no_config_is_a_no_op(self):
        self._write_existing()
        before = self.target.read_text()
        self._configure("--no-config")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cs.update_saved_config(["node"])
        self.assertEqual(self.target.read_text(), before)
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_dry_run_reports_an_existing_file_without_writing(self):
        self._write_existing()
        before = self.target.read_text()
        cs.DRY_RUN = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cs.update_saved_config(["node"])
        self.assertIn(f"Config would be updated: {self.target}", out.getvalue())
        self.assertEqual(self.target.read_text(), before)

    def test_dry_run_warns_when_there_is_no_file(self):
        cs.DRY_RUN = True
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cs.update_saved_config(["node"])
        self.assertIn("no saved config for sandbox 'demo'", err.getvalue())

    def test_a_changed_profile_directory_is_recorded(self):
        # A profile applied from a non-default directory must be replayable by
        # --recreate, so the directory is recorded with the name.
        self._write_existing()
        profile_dir = self.root / "my-profiles"
        self._configure("-profile-directory", str(profile_dir))
        cs.update_saved_config(["node"])
        data = cs.load_config(str(self.target))
        self.assertEqual(data["profile_directory"], str(profile_dir))
        self.assertEqual(data["profiles"], ["python", "node"])

    def test_duplicate_names_are_recorded_once(self):
        self._write_existing()
        cs.update_saved_config(["node", "node"])
        self.assertEqual(cs.load_config(str(self.target))["profiles"], ["python", "node"])


class ApplyLoadedConfigTests(unittest.TestCase):
    def setUp(self):
        self._config = cs.CONFIG
        self.addCleanup(self._restore)

    def _restore(self):
        cs.CONFIG = self._config

    DATA = {
        "name": "demo",
        "project": "/projects/demo",
        "env_file": "/secrets/sbx.env",
        "scan_secrets": "false",
        "secrets_settings": "/secrets/scan.json",
        "profiles": ["python", "node"],
        "profile_directory": "/profiles",
        "volumes": ["/data:rw", "/docs"],
    }

    def test_every_field_is_copied_into_the_global_config(self):
        cs.CONFIG = cs.Config()
        cs.apply_loaded_config(self.DATA)
        self.assertEqual(cs.CONFIG.name, "demo")
        self.assertEqual(cs.CONFIG.path, "/projects/demo")
        self.assertEqual(cs.CONFIG.envfile, "/secrets/sbx.env")
        self.assertEqual(cs.CONFIG.secrets, "false")
        self.assertEqual(cs.CONFIG.secretsfile, "/secrets/scan.json")
        self.assertEqual(cs.CONFIG.profiles, "python,node")
        self.assertEqual(cs.CONFIG.profile_directory, "/profiles")
        self.assertEqual(cs.CONFIG.volumes, ["/data:rw", "/docs"])

    def test_a_name_mismatch_aborts_before_anything_is_copied(self):
        cs.CONFIG = cs.Config()
        with self.assertRaises(cs.ConfigError) as caught:
            cs.apply_loaded_config(self.DATA, expected_name="other")
        message = str(caught.exception)
        self.assertIn("'demo'", message)
        self.assertIn("'other'", message)
        self.assertEqual(cs.CONFIG.name, "")
        self.assertEqual(cs.CONFIG.path, "")

    def test_a_matching_expected_name_is_accepted(self):
        cs.CONFIG = cs.Config()
        cs.apply_loaded_config(self.DATA, expected_name="demo")
        self.assertEqual(cs.CONFIG.name, "demo")


class MainModeTests(unittest.TestCase):
    """The main() branches for --recreate and --from-config.

    Everything that would touch a real sandbox is patched; these tests assert the
    commands main() would issue and the messages it prints.
    """

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
        self, name="s", scan_secrets="false", volumes=(), profiles=(), profile_directory=""
    ):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / f"{name}.yaml"
        target.write_text(
            cs.render_config({
                "name": name,
                "project": str(self.project),
                "scan_secrets": scan_secrets,
                "volumes": list(volumes),
                "profiles": list(profiles),
                "profile_directory": profile_directory,
            })
        )
        return target

    def test_recreate_without_force_refuses_before_any_sbx_command(self):
        self._saved_config()
        err = io.StringIO()
        with mock.patch.object(cs, "have", return_value=True), mock.patch.object(
            cs, "existing_sandbox_names", return_value=["s"]
        ), mock.patch.object(cs, "sbx") as fake_sbx, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cs.main(["-n", "s", "--recreate", "--config-dir", str(self.config_dir)])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--force", err.getvalue())
        fake_sbx.assert_not_called()

    def test_recreate_with_force_removes_then_creates_in_order(self):
        self._saved_config()
        with mock.patch.object(cs, "have", return_value=True), mock.patch.object(
            cs, "existing_sandbox_names", return_value=["s"]
        ), mock.patch.object(cs, "wait_for_sandbox", return_value=True), mock.patch.object(
            cs, "setup_sandbox"
        ), mock.patch.object(cs, "update_sbx_policy"), mock.patch.object(
            cs, "update_sbx_policy_slack"
        ), mock.patch.object(cs, "update_sbx_policy_nctalk"), mock.patch.object(
            cs, "apply_profiles"
        ), mock.patch.object(
            cs, "save_created_config", return_value=""
        ), mock.patch.object(cs, "sbx") as fake_sbx, contextlib.redirect_stdout(
            io.StringIO()
        ):
            rc = cs.main(["-n", "s", "--recreate", "--force",
                          "--config-dir", str(self.config_dir)])
        self.assertEqual(rc, 0)
        calls = [call[0][0] for call in fake_sbx.call_args_list]
        self.assertEqual(calls[0], ["rm", "s"])
        self.assertEqual(calls[1], ["create", "--name", "s", "claude", str(self.project)])

    def test_recreate_force_dry_run_echoes_the_removal(self):
        # --dry-run promises to print every command that would run; the removal is
        # destructive and therefore the most important one to show.
        self._saved_config()
        with mock.patch.object(cs, "sbx") as fake_sbx, contextlib.redirect_stdout(
            io.StringIO()
        ):
            rc = cs.main(["-n", "s", "--recreate", "--force", "--dry-run",
                          "--config-dir", str(self.config_dir)])
        self.assertEqual(rc, 0)
        calls = [call[0][0] for call in fake_sbx.call_args_list]
        self.assertEqual(calls[0], ["rm", "s"])
        self.assertEqual(calls[1][0:2], ["create", "--name"])

    def test_from_config_dry_run_echoes_the_create_command(self):
        volume = self.root / "data"
        volume.mkdir()
        target = self._saved_config(volumes=[f"{volume}:rw"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cs.main(["--from-config", str(target), "--dry-run",
                          "--config-dir", str(self.config_dir)])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("  $ sbx create --name s claude", text)
        self.assertIn(str(self.project), text)
        self.assertIn(str(volume), text)

    def test_a_profile_bundle_cannot_install_managed_files(self):
        profile_dir = self.root / "profiles"
        bundle = profile_dir / "bad"
        bundle.mkdir(parents=True)
        (bundle / "profile.yaml").write_text(
            "name: bad\n"
            "description: installs a managed file\n"
            "policies:\n"
            "  network:\n"
            "    allow:\n"
            "      - example.com\n"
        )
        (bundle / "settings.json").write_text("{}")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ), self.assertRaises(SystemExit) as ctx:
            cs.main(["-n", "s", "-p", str(self.project), "-s", "false",
                     "--dry-run", "-profile", "bad",
                     "-profile-directory", str(profile_dir),
                     "--config-dir", str(self.config_dir)])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("settings.json", err.getvalue())
        self.assertIn("agent level", err.getvalue())


if __name__ == "__main__":
    unittest.main()
