#!/usr/bin/env python3
"""Unit tests for docker-sbx-create-sandbox.py (standard library only).

Covers the pure helpers the CLI relies on: argument parsing, the env-file reader,
volume specs, Slack validation, host extraction, the secrets-file scanner and the
minimal YAML profile parser. The end-to-end behaviour is covered separately by
test/run-tests.sh, which runs the same assertions against the bash and Python
implementations.

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
