#!/usr/bin/env python3
"""Unit tests for docker-sbx-update-sandboxes.py (standard library only).

Covers the pure helpers the CLI relies on: list normalisation, argument parsing
(including the bash-compatible exit-2 messages), the apt chain, exec-flag
detection, and the three sandbox-name extraction paths. Nothing here needs a real
`sbx` or a sandbox.

    python3 -m unittest discover -s test -p 'test_*.py'
    (or, from inside test/:  python3 -m unittest test_update_sandboxes)
"""

import importlib.util
import unittest
from pathlib import Path

# The module name has hyphens, so it cannot be imported by name. The script sits
# in the repository root, one level up from this test/ directory.
REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "docker_sbx_update_sandboxes", REPO / "docker-sbx-update-sandboxes.py"
)
us = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(us)


class NormalizeListTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(us.normalize_list(""), [])

    def test_commas_and_spaces(self):
        self.assertEqual(us.normalize_list("bc,web-1"), ["bc", "web-1"])
        self.assertEqual(us.normalize_list("bc, web-1 , api"), ["bc", "web-1", "api"])

    def test_runs_of_whitespace_and_tabs(self):
        self.assertEqual(us.normalize_list("  a \t b  \n c "), ["a", "b", "c"])

    def test_order_and_duplicates_kept(self):
        # bash word-splits without de-duplicating, and the "Including N: a b c"
        # line prints them in order, duplicates included.
        self.assertEqual(us.normalize_list("b,a,b"), ["b", "a", "b"])


class CliTests(unittest.TestCase):
    def test_defaults(self):
        cfg = us.parse_args([], env={})
        self.assertEqual(cfg.include_raw, "")
        self.assertEqual(cfg.exclude_raw, "")
        self.assertEqual(cfg.dry_run, "0")
        self.assertEqual(cfg.cloud, "0")
        self.assertEqual(cfg.apt, "apt-get")
        self.assertEqual(cfg.sudo, "sudo")
        self.assertEqual(cfg.sbx_timeout, "120")
        self.assertFalse(cfg.show_help)

    def test_help_flags(self):
        self.assertTrue(us.parse_args(["-h"], env={}).show_help)
        self.assertTrue(us.parse_args(["--help"], env={}).show_help)

    def test_dry_run_flag(self):
        self.assertEqual(us.parse_args(["--dry-run"], env={}).dry_run, "1")

    def test_dry_run_env(self):
        self.assertEqual(us.parse_args([], env={"DRY_RUN": "1"}).dry_run, "1")

    def test_include_forms(self):
        for argv in (["-i", "bc,web-1"], ["--include", "bc,web-1"], ["--include=bc,web-1"]):
            self.assertEqual(us.parse_args(argv, env={}).include, ["bc", "web-1"], argv)

    def test_exclude_forms(self):
        for argv in (["-x", "bc"], ["--exclude", "bc"], ["--exclude=bc"]):
            self.assertEqual(us.parse_args(argv, env={}).exclude, ["bc"], argv)

    def test_repeatable_and_merged_with_env(self):
        cfg = us.parse_args(["-i", "web-1", "-i", "api"], env={"INCLUDE": "bc"})
        self.assertEqual(cfg.include, ["bc", "web-1", "api"])

    def test_env_seeds_the_lists(self):
        cfg = us.parse_args([], env={"INCLUDE": "a,b", "SKIP_LIST": "c d"})
        self.assertEqual(cfg.include, ["a", "b"])
        self.assertEqual(cfg.exclude, ["c", "d"])

    def test_double_dash_is_ignored(self):
        cfg = us.parse_args(["--", "-i", "bc"], env={})
        self.assertEqual(cfg.include, ["bc"])

    def test_unknown_option_exits_2(self):
        with self.assertRaises(us.UsageError) as ctx:
            us.parse_args(["-z"], env={})
        self.assertEqual(str(ctx.exception), "unknown option: -z")

    def test_missing_value_exits_2(self):
        for argv in (["-i"], ["--exclude"]):
            with self.assertRaises(us.UsageError) as ctx:
                us.parse_args(argv, env={})
            self.assertEqual(str(ctx.exception), f"missing sandbox list after {argv[0]}")

    def test_positional_argument_exits_2(self):
        with self.assertRaises(us.UsageError) as ctx:
            us.parse_args(["bc"], env={})
        self.assertEqual(
            str(ctx.exception), "unexpected argument: bc (use -i/--include to name sandboxes)"
        )


class ConfigTests(unittest.TestCase):
    def test_sbx_prefix(self):
        self.assertEqual(us.build_sbx(us.Config({})), ["sbx"])
        self.assertEqual(us.build_sbx(us.Config({"CLOUD": "1"})), ["sbx", "--cloud"])

    def test_apt_chain_default(self):
        chain = us.build_apt_chain(us.Config({}))
        self.assertIn("apt update", chain)
        self.assertIn("apt-get -y upgrade", chain)
        self.assertIn("apt-get -y autoremove", chain)

    def test_debian_frontend_after_sudo(self):
        # sudo's env_reset would strip an assignment placed *before* it.
        chain = us.build_apt_chain(us.Config({}))
        self.assertIn("sudo DEBIAN_FRONTEND=noninteractive apt-get update", chain)
        self.assertNotIn("DEBIAN_FRONTEND=noninteractive sudo", chain)

    def test_sudo_and_apt_are_substituted(self):
        chain = us.build_apt_chain(us.Config({"SUDO": "sudo -n", "APT": "apt"}))
        self.assertIn("sudo -n DEBIAN_FRONTEND=noninteractive apt update", chain)
        self.assertNotIn("$SUDO", chain)
        self.assertNotIn("$APT", chain)

    def test_custom_apt_chain_wins(self):
        cfg = us.Config({"APT_CHAIN": "echo custom"})
        self.assertEqual(us.build_apt_chain(cfg), "echo custom")

    def test_exec_flags_detection(self):
        self.assertEqual(us.detect_exec_flags("", True, True), "-ti")
        self.assertEqual(us.detect_exec_flags("", True, False), "-i")
        self.assertEqual(us.detect_exec_flags("", False, False), "-i")

    def test_explicit_exec_flags_win(self):
        self.assertEqual(us.detect_exec_flags("-i --env X=1", True, True), "-i --env X=1")


class NameExtractionTests(unittest.TestCase):
    def test_clean_names_trims_dedupes_sorts(self):
        self.assertEqual(us.clean_names("  b \n\na\na\n"), ["a", "b"])

    def test_json_top_level_list(self):
        text = '[{"name": "web-1"}, {"name": "bc"}]'
        self.assertEqual(us.extract_json_names(text), ["web-1", "bc"])

    def test_json_nested_and_capitalised_key(self):
        text = '{"sandboxes": [{"Name": "a"}, {"meta": {"name": "b"}}]}'
        self.assertEqual(sorted(us.extract_json_names(text)), ["a", "b"])

    def test_json_lowercase_key_wins(self):
        # jq's `.name // .Name`: the first of the two that is not null/false.
        self.assertEqual(us.extract_json_names('{"name": "a", "Name": "b"}'), ["a"])

    def test_json_non_string_and_empty_ignored(self):
        text = '{"name": 42, "Name": "", "other": "x"}'
        self.assertEqual(us.extract_json_names(text), [])

    def test_json_invalid(self):
        self.assertEqual(us.extract_json_names("not json"), [])

    def test_table_skips_header_and_rule_rows(self):
        text = "\n".join(
            [
                "NAME        ID          AGENT",
                "-------------------------------",
                "web-1       abc123      claude",
                "bc          def456      claude",
                "",
            ]
        )
        self.assertEqual(us.parse_table_names(text), ["bc", "web-1"])

    def test_table_skips_unicode_rule_rows(self):
        text = "NAME   ID\n─────  ──\nweb-1  abc\n"
        self.assertEqual(us.parse_table_names(text), ["web-1"])


if __name__ == "__main__":
    unittest.main()
