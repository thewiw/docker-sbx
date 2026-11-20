#!/usr/bin/env python3
"""docker-sbx sandbox creation - cross-platform.

Creates a Docker Sandboxes (`sbx`) sandbox for a project: scans the project for
secrets, creates the sandbox, stages the in-sandbox setup script into it, then
applies the network policy the sandbox needs (model provider, optional Slack)
and any requested policy profiles.

This is the cross-platform successor to docker-sbx-create-sandbox.sh, which was
Linux-only (it used POSIX `find` and assumed `/`-rooted paths). The function
names below deliberately mirror the bash ones so the two stay easy to diff while
both are in the tree.

Only the standard library is used. Profiles are YAML: PyYAML is used when it
happens to be importable, otherwise a small parser covers the documented profile
subset (see parse_profile_minimal).

Usage:
    python3 docker-sbx-create-sandbox.py -n NAME -p /absolute/project/path [options]

    python3 docker-sbx-create-sandbox.py --help
    python3 docker-sbx-create-sandbox.py --dry-run -n test -p /tmp/project

Applying profiles to an existing sandbox is a separate command:
    python3 docker-sbx-apply-profile.py -n NAME -profile NAMES
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SBX_HOME = "/home/agent"

# How long a brand-new sandbox's first start may take before setup gives up on it,
# how often to re-check, and how often to say so. The first start is when the guest
# gets its agent (sbx downloads it there) and when the sandbox's own startup hooks
# run apt, so the wait is normally dominated by that download - hence the generous
# timeout.
SANDBOX_READY_TIMEOUT = 600
SANDBOX_READY_POLL = 5
SANDBOX_READY_REPORT = 30

DEFAULT_SEARCH_PATTERNS = (
    "google-services.json",
    "key.properties",
    "*.jks",
    "*.cert",
    "*.crt",
    "*.key",
)
DEFAULT_AVOID_PATTERNS = (".venv", "node_modules", "bower_components")

GITLEAKS_IMAGE = "ghcr.io/gitleaks/gitleaks:latest"

SUPPORTED_SLACK_EVENTS = (
    "Stop",
    "Notification",
    "SessionStart",
    "SessionEnd",
    "SubagentStop",
    "UserPromptSubmit",
)
DEFAULT_SLACK_EVENTS = "Stop,Notification,SessionStart,SessionEnd"

# Nextcloud Talk notifications share the same event vocabulary and default set.
SUPPORTED_NCTALK_EVENTS = SUPPORTED_SLACK_EVENTS
DEFAULT_NCTALK_EVENTS = DEFAULT_SLACK_EVENTS

# A profile may never imply a global policy - it is always per-sandbox.
FORBIDDEN_PROFILE_KEYS = ("global", "default", "all_sandboxes", "sandbox_default")

# The documented top-level keys. Anything else is refused rather than ignored: a
# typo'd `policy:` used to be silently dropped, creating a sandbox with no rules.
PROFILE_KEYS = ("name", "description", "policies")

# A policy entry names a host, optionally wildcarded ("*.example.com"). Syntax
# only: `sbx policy` remains the authority, but a URL or a path is never a domain.
PROFILE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9*](?:[A-Za-z0-9.*_-]*[A-Za-z0-9*])?$")

# A profile directory carries an asset tree that mirrors the sandbox's ~/.claude.
# These filenames name the profile definition itself and are never copied.
PROFILE_DEFINITION_NAMES = ("profile.yaml", "profile.yml")

# Files a profile may never install, because docker-sbx or Claude Code owns them.
# The check is on the first path component, so a directory is covered too. `.git`
# is refused at both levels: a bundle pointed at a checkout must not ship its
# history into the agent's home or the shared project volume.
AGENT_ASSET_DENY = ("settings.json", "settings.local.json", ".credentials.json", ".git")
PROJECT_ASSET_DENY = (".credentials.json", ".git")

# Safety rail against selecting a whole checkout by accident.
ASSET_MAX_FILES = 2000
ASSET_MAX_BYTES = 32 * 1024 * 1024

# Per-sandbox recreate config ("docker-compose.yml" for one sandbox).
CONFIG_VERSION = 1
CONFIG_KEYS = (
    "version",
    "name",
    "project",
    "env_file",
    "scan_secrets",
    "secrets_settings",
    "profiles",
    "profile_directory",
    "volumes",
)

DRY_RUN = False


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def banner(text: str) -> None:
    line = "═" * max(len(text) + 4, 52)
    print()
    print(line)
    print(text)
    print(line)


def info(msg: str = "") -> None:
    print(msg)


def _flush_stdout() -> None:
    """Keep stderr in step with stdout.

    stdout is block-buffered when the output is piped, while stderr is not, so a
    warning or error would otherwise surface *before* the lines explaining it.
    """
    sys.stdout.flush()


def warn(msg: str) -> None:
    _flush_stdout()
    print(f"Warning: {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    _flush_stdout()
    print(f"Error: {msg}", file=sys.stderr)
    print()
    sys.exit(1)


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------


class _DryResult:
    """Stand-in for a CompletedProcess while --dry-run is active."""

    returncode = 0
    stdout = ""
    stderr = ""


def run(cmd, *, check: bool = True, capture: bool = False, quiet: bool = False):
    """Run a command, echoing it first. Aborts on failure when check is set.

    quiet suppresses the echo for the commands that are polled (the readiness
    probe), where one line per attempt would bury the rest of the output.
    """
    cmd = [str(c) for c in cmd]
    if not quiet:
        info("  $ " + " ".join(shlex.quote(c) for c in cmd))
    if DRY_RUN:
        return _DryResult()
    try:
        proc = subprocess.run(cmd, text=True, capture_output=capture)
    except FileNotFoundError:
        # subprocess raises rather than reporting a status when the binary is not on
        # PATH, which surfaced as a bare traceback the moment `sbx` was missing.
        if check:
            die(f"command not found: {cmd[0]} (is it installed and on PATH?)")
        # check=False callers handle return codes, so answer in the same currency -
        # 127 is what a shell reports for a command it cannot find.
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")
    if check and proc.returncode != 0:
        die(f"command failed with exit code {proc.returncode}: " + " ".join(cmd))
    return proc


def have(command: str) -> bool:
    return shutil.which(command) is not None


def sbx(args, *, check: bool = False):
    """Run `sbx <args>`. Most sbx calls are reported, not fatal, by their caller."""
    return run(["sbx", *args], check=check)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


class CliError(Exception):
    """A command-line problem worth printing, with or without the usage text."""


class Config:
    def __init__(self) -> None:
        self.name = ""
        self.path = ""
        self.envfile = ""
        self.secrets = "true"
        self.secretsfile = ""
        self.gensecretsfile = ""
        self.volumes = []
        self.profiles = ""
        self.profile_directory = ""
        self.from_config = ""
        self.config_dir = ""
        self.recreate = False
        self.force = False
        self.no_config = False
        self.dry_run = False
        self.show_help = False
        # Every attribute the user actually named on the command line. Mode
        # validation uses it to reject create-only flags instead of silently
        # ignoring values that would have no effect.
        self.seen = set()


def usage_text(mode: str = "create") -> str:
    scriptname = Path(sys.argv[0]).name
    if mode == "update":
        return "\n".join(
            [
                f"Usage: {scriptname} -n [sandbox name] -profile [profile names] [options]",
                "Apply policy profiles (network rules and Claude assets) to an existing sandbox,",
                "then record them in the sandbox's saved config.",
                "Options:",
                "  -n string               Sandbox to update [mandatory]",
                "  -profile names          Comma-separated profiles to apply, in order [mandatory]",
                "  -profile-directory path Directory containing profile YAML files [optional, default: ./profiles]",
                "",
                "  --config-dir dir  Directory holding <sandbox>.yaml config files [optional]",
                "  --no-config       Do not update the sandbox's saved config",
                "  --dry-run         Print every command that would run, and change nothing",
                "  -h, --help        Show this help and exit",
                "",
                "Example(s):",
                f"  {scriptname} -n test -profile node",
                f"  {scriptname} -n test -profile python,node -profile-directory ~/my-profiles",
                "",
            ]
        )
    return "\n".join(
        [
            f"Usage: {scriptname} -n [sandbox name] -p [project path] -e [sandbox env file] -s [check secrets] -sf [secrets config file]",
            "Options:",
            "  -n string     Define the name of the new sandbox [mandatory]",
            "  -p string     Absolute path of project's directory [mandatory]",
            "  -e string     Env file [optional]",
            "  -s true/false Check for secrets (default true)",
            "  -sf string    Secrets config JSON file [optional]",
            "  -gsf string   Generate a default secrets config JSON file and exit",
            "  -v path       Mount an extra host directory/file into the sandbox (absolute path).",
            "                Repeat -v for multiple mounts. Read-only by default; append :rw to",
            "                mount read-write (or :ro to be explicit). e.g. -v /mnt/c/docs -v /mnt/c/data:rw",
            "  -profile names Comma-separated list of policy profiles to apply (order matters) [optional]",
            "  -profile-directory path Directory containing profile YAML files [optional, default: ./profiles]",
            "",
            "  --recreate        Rebuild sandbox -n from its saved config (refuses if it exists)",
            "  --force           With --recreate: remove the existing sandbox first (sbx rm)",
            "  --from-config f   Create from a saved config file instead of -n/-p/...",
            "  --config-dir dir  Directory holding <sandbox>.yaml config files",
            "                    [default: ~/.config/docker-sbx/sandboxes, or the platform equivalent]",
            "  --no-config       Do not write a sandbox config file",
            "",
            "  --dry-run     Print every command that would run, and change nothing",
            "  -h, --help    Show this help and exit",
            "",
            "To add profiles to a sandbox that already exists, use docker-sbx-apply-profile.py.",
            "",
            "Example(s):",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -e sbx.env",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -e sbx.env -sf secrets.json",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -v /mnt/c/docs -v /mnt/c/data:rw",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -profile python",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -profile python,java",
            f"  from WSL : {scriptname} -gsf secrets.json",
            f"  recreate : {scriptname} -n test --recreate --force",
            f"  from file: {scriptname} --from-config ~/.config/docker-sbx/sandboxes/test.yaml",
            "",
        ]
    )


def usage(exit_code: int = 1, mode: str = "create") -> "None":
    print(usage_text(mode))
    sys.exit(exit_code)


# Options that consume the following argument.
_VALUE_OPTIONS = {
    "-n": "name",
    "-p": "path",
    "-e": "envfile",
    "-s": "secrets",
    "-sf": "secretsfile",
    "-gsf": "gensecretsfile",
    "-v": "volumes",
    "-profile": "profiles",
    "-profile-directory": "profile_directory",
    "--from-config": "from_config",
    "--config-dir": "config_dir",
}

# Options that are just switches.
_BOOLEAN_OPTIONS = {
    "--dry-run": "dry_run",
    "--recreate": "recreate",
    "--force": "force",
    "--no-config": "no_config",
}


def parse_args(argv):
    """Parse the arguments, mirroring the bash while-loop and its messages.

    Raises CliError for an unknown option so the caller can print the usage,
    exactly as the bash version does.
    """
    cfg = Config()
    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg in ("-h", "--help"):
            cfg.show_help = True
            return cfg
        boolean = _BOOLEAN_OPTIONS.get(arg)
        if boolean is not None:
            setattr(cfg, boolean, True)
            cfg.seen.add(boolean)
            continue
        attribute = _VALUE_OPTIONS.get(arg)
        if attribute is None:
            raise CliError(f"Unknown option: {arg}")
        if index >= len(argv):
            raise CliError(f"Option {arg} requires a value")
        value = argv[index]
        index += 1
        if attribute == "volumes":
            cfg.volumes.append(value)
        else:
            setattr(cfg, attribute, value)
        cfg.seen.add(attribute)
    return cfg


def select_mode(cfg: Config) -> str:
    """create (default), recreate, or from-config."""
    chosen = [
        (flag, mode)
        for flag, mode, enabled in (
            ("--recreate", "recreate", cfg.recreate),
            ("--from-config", "from-config", bool(cfg.from_config)),
        )
        if enabled
    ]
    if len(chosen) > 1:
        names = ", ".join(flag for flag, _ in chosen)
        raise CliError(f"Only one of {names} may be used at a time")
    return chosen[0][1] if chosen else "create"


# Flags that describe how a sandbox is *created*; they have no meaning when
# adding profiles to a live sandbox or rebuilding one from its config.
_CREATE_FLAGS = {
    "path": "-p",
    "envfile": "-e",
    "secrets": "-s",
    "secretsfile": "-sf",
    "gensecretsfile": "-gsf",
    "volumes": "-v",
}


def validate_mode_flags(cfg: Config, mode: str) -> None:
    """Reject flags that the selected mode cannot honour, rather than ignore them."""
    if cfg.force and mode != "recreate":
        raise CliError("--force is only valid with --recreate")
    # Create mode accepts its full historical flag set; only the other modes are
    # narrowed.
    if mode == "create":
        return
    mode_flag = {
        "update": "the apply-profile command",
        "recreate": "--recreate",
        "from-config": "--from-config",
    }[mode]
    allowed = {
        "update": {"name", "profiles", "profile_directory", "config_dir", "dry_run", "no_config"},
        "recreate": {"name", "config_dir", "dry_run", "no_config", "recreate", "force"},
        "from-config": {"from_config", "config_dir", "dry_run", "no_config"},
    }[mode]
    rejected = sorted(cfg.seen - allowed)
    if not rejected:
        return
    attribute = rejected[0]
    if attribute in _CREATE_FLAGS:
        shown = _CREATE_FLAGS[attribute]
    else:
        shown = next(
            (flag for flag, attr in {**_VALUE_OPTIONS, **_BOOLEAN_OPTIONS}.items() if attr == attribute),
            attribute,
        )
    raise CliError(f"Option {shown} cannot be used with {mode_flag}")


# --------------------------------------------------------------------------
# Per-sandbox recreate config
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """A sandbox config file that cannot be read, parsed or written."""


def default_config_dir() -> Path:
    """Where <sandbox>.yaml lives when --config-dir is not given."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "docker-sbx" / "sandboxes"


def config_dir() -> Path:
    """The config directory: --config-dir when given, the platform default otherwise."""
    return Path(CONFIG.config_dir) if CONFIG.config_dir else default_config_dir()


def config_file(sandbox_name: str) -> Path:
    """The config path for one sandbox, refusing names that would escape the dir."""
    invalid = not sandbox_name or sandbox_name in (".", "..") or any(
        char in sandbox_name for char in ("/", "\\")
    )
    if os.name == "nt" and ":" in sandbox_name:
        invalid = True
    if invalid:
        raise ConfigError(f"sandbox name '{sandbox_name}' cannot be used as a config file name")
    return config_dir() / f"{sandbox_name}.yaml"


def _yaml_string(value) -> str:
    """Single-quoted YAML scalar. '' is the only escape YAML single quotes need."""
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ConfigError(f"value {text!r} cannot be stored in a sandbox config (newline)")
    return "'" + text.replace("'", "''") + "'"


def render_config(values: dict) -> str:
    """Emit the documented block-style subset, in a stable key order."""
    lines = [
        f"version: {CONFIG_VERSION}",
        f"name: {_yaml_string(values.get('name', ''))}",
        f"project: {_yaml_string(values.get('project', ''))}",
    ]
    if values.get("env_file"):
        lines.append(f"env_file: {_yaml_string(values['env_file'])}")
    scan = "false" if str(values.get("scan_secrets", "true")).lower() == "false" else "true"
    lines.append(f"scan_secrets: {scan}")
    if values.get("secrets_settings"):
        lines.append(f"secrets_settings: {_yaml_string(values['secrets_settings'])}")
    if values.get("profiles"):
        lines.append("profiles:")
        lines.extend(f"  - {_yaml_string(profile)}" for profile in values["profiles"])
    if values.get("profile_directory"):
        lines.append(f"profile_directory: {_yaml_string(values['profile_directory'])}")
    if values.get("volumes"):
        lines.append("volumes:")
        lines.extend(f"  - {_yaml_string(volume)}" for volume in values["volumes"])
    return "\n".join(lines) + "\n"


def _config_bool(value, where: str) -> str:
    """Normalise PyYAML's bool and the minimal parser's string to "true"/"false"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower()
    raise ConfigError(f"{where} 'scan_secrets' must be true or false")


def _config_string(value, key: str, where: str, *, required: bool = False) -> str:
    """A string field, or "" when optional and absent; rejects non-strings."""
    if value is None:
        if required:
            raise ConfigError(f"{where} is missing '{key}'")
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where} '{key}' must be a string")
    if required and not value.strip():
        raise ConfigError(f"{where} '{key}' must not be empty")
    return value


def _config_string_list(value, key: str, where: str):
    """A list of non-empty strings, or [] when absent."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where} '{key}' must be a list")
    result = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"{where} '{key}' contains an invalid entry")
        if entry.strip() not in result:
            result.append(entry.strip())
    return result


def validate_config(data, where: str) -> dict:
    """Schema check for a sandbox config; returns a normalised mapping."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a YAML mapping")

    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ConfigError(f"{where} contains unknown key '{unknown[0]}'")
    version = data.get("version")
    if version is None or str(version) != str(CONFIG_VERSION):
        raise ConfigError(
            f"{where} has unsupported version {version!r} (expected {CONFIG_VERSION})"
        )

    name = _config_string(data.get("name"), "name", where, required=True)
    project = _config_string(data.get("project"), "project", where, required=True)
    if not os.path.isabs(project):
        raise ConfigError(f"{where} 'project' must be an absolute path")

    paths = {}
    for key in ("env_file", "secrets_settings", "profile_directory"):
        value = _config_string(data.get(key), key, where)
        if value and not os.path.isabs(value):
            raise ConfigError(f"{where} '{key}' must be an absolute path")
        paths[key] = value

    return {
        "name": name.strip(),
        "project": project,
        "env_file": paths["env_file"],
        "scan_secrets": _config_bool(data.get("scan_secrets", "true"), where),
        "secrets_settings": paths["secrets_settings"],
        "profiles": _config_string_list(data.get("profiles"), "profiles", where),
        "profile_directory": paths["profile_directory"],
        "volumes": _config_string_list(data.get("volumes"), "volumes", where),
    }


def load_config(config_path: str) -> dict:
    """Read and validate one sandbox config, PyYAML when available."""
    where = f"config {config_path}"
    try:
        text = Path(config_path).read_text()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"failed to read {where}: {exc}")

    try:
        import yaml  # noqa: F401  (optional: full YAML when installed)
    except ImportError:
        try:
            data = parse_profile_minimal(text, where)
        except ProfileError as exc:
            raise ConfigError(str(exc))
    else:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ConfigError(f"failed to parse {where}: {exc}")
    return validate_config(data, where)


def write_config_file(target: Path, text: str) -> None:
    """Atomic write: a crash mid-write must not leave a half config behind."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", newline="\n") as handle:
                handle.write(text)
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, target)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except OSError as exc:
        raise ConfigError(f"failed to write {target}: {exc}")


def current_config_values() -> dict:
    """Everything needed to recreate the sandbox being created, minus credentials."""
    return {
        "name": name(),
        "project": os.path.abspath(path()),
        "env_file": os.path.abspath(envfile()) if envfile() else "",
        "scan_secrets": "false" if secrets().lower() == "false" else "true",
        "secrets_settings": os.path.abspath(secretsfile()) if secretsfile() else "",
        "profiles": split_profile_names(profiles()),
        # Omitted when the default ./profiles is in use (the option is simply
        # empty), so the file stays portable across checkouts; an explicit
        # -profile-directory - or one loaded from another config - is recorded
        # absolutely.
        "profile_directory": (
            os.path.abspath(profile_directory()) if profile_directory() else ""
        ),
        "volumes": list(volumes()),
    }


def save_created_config() -> str:
    """Write the sandbox's config after a successful create. Returns its path."""
    if CONFIG.no_config:
        return ""
    try:
        target = config_file(name())
        text = render_config(current_config_values())
    except ConfigError as exc:
        warn(str(exc))
        return ""
    if DRY_RUN:
        info(f"Config would be saved: {target}")
        for line in text.splitlines():
            info(f"    {line}")
        return ""
    try:
        write_config_file(target, text)
    except ConfigError as exc:
        # The sandbox exists; losing its recreate record is not worth failing the
        # whole run for.
        warn(str(exc))
        return ""
    # Printed by the caller, as the last line of the run.
    return str(target)


def _set_config_scalar_text(text: str, key: str, value: str):
    """Set a top-level scalar key in place, inserting it before volumes: if new."""
    replacement = f"{key}: {_yaml_string(value)}"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            if line.rstrip() == replacement:
                return text
            lines[index] = replacement
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    for index, line in enumerate(lines):
        if line.startswith("volumes:"):
            lines.insert(index, replacement)
            break
    else:
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def add_profiles_to_config_text(text: str, profile_names, profile_directory=None):
    """Append missing names to the profiles: block, preserving every other byte.

    An inline flow list (`profiles: [python]`) is rewritten into the block form,
    which is the subset the dependency-free parser can read. `profile_directory`,
    when given, is recorded in the same pass. Returns None for any other form this
    line surgery cannot handle, so the caller can fall back to parse-and-re-render.
    """
    names = []
    for item in profile_names:
        item = item.strip() if isinstance(item, str) else ""
        if item and item not in names:
            names.append(item)
    lines = text.splitlines()
    start = None
    trailing = "\n" if text.endswith("\n") else ""
    for index, line in enumerate(lines):
        if not line.startswith("profiles:"):
            continue
        inline = line[len("profiles:") :].strip()
        if inline:
            if not (inline.startswith("[") and inline.endswith("]")):
                return None
            parts = [part.strip() for part in inline[1:-1].split(",") if part.strip()]
            try:
                merged = [_scalar(part, "profiles") for part in parts]
            except ProfileError:
                return None
            for item in names:
                if item not in merged:
                    merged.append(item)
            lines[index : index + 1] = ["profiles:"] + [
                f"  - {_yaml_string(item)}" for item in merged
            ]
            result = "\n".join(lines) + trailing
            return (
                _set_config_scalar_text(result, "profile_directory", profile_directory)
                if profile_directory
                else result
            )
        start = index
        break

    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("profiles:")
        lines.extend(f"  - {_yaml_string(item)}" for item in names)
        result = "\n".join(lines) + "\n"
    else:
        existing = []
        last_item = start
        index = start + 1
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if not line.startswith((" ", "\t")):
                break
            match = re.match(r"^\s+-\s+(.*)$", line)
            if match:
                try:
                    existing.append(_scalar(match.group(1).strip(), "profiles"))
                except ProfileError:
                    pass
                last_item = index
            index += 1

        missing = [item for item in names if item not in existing]
        if missing:
            lines[last_item + 1 : last_item + 1] = [
                f"  - {_yaml_string(item)}" for item in missing
            ]
        result = "\n".join(lines) + trailing

    if profile_directory:
        result = _set_config_scalar_text(result, "profile_directory", profile_directory)
    return result


def update_saved_config(profile_names) -> None:
    """Record newly applied profiles (and their directory) in the saved config."""
    if CONFIG.no_config:
        return
    try:
        target = config_file(name())
    except ConfigError as exc:
        warn(str(exc))
        return

    if DRY_RUN:
        if target.is_file():
            info(f"Config would be updated: {target}")
        else:
            warn(f"no saved config for sandbox '{name()}' at {target}")
        return

    if not target.is_file():
        warn(
            f"no saved config for sandbox '{name()}' at {target}; "
            "the applied profiles are not recorded"
        )
        return

    # The directory a profile was applied from must be recorded too, or a later
    # --recreate resolves the same names against the default ./profiles.
    effective_directory = os.path.abspath(profile_directory()) if profile_directory() else ""
    try:
        text = target.read_text()
        updated = add_profiles_to_config_text(
            text, profile_names, effective_directory or None
        )
        if updated is None:
            # Unusual hand-edited form: preserve the values, re-render canonically.
            data = load_config(str(target))
            merged = list(data["profiles"])
            for item in profile_names:
                item = item.strip()
                if item and item not in merged:
                    merged.append(item)
            data["profiles"] = merged
            if effective_directory:
                data["profile_directory"] = effective_directory
            updated = render_config(data)
        write_config_file(target, updated)
    except (OSError, UnicodeError, ConfigError) as exc:
        warn(f"could not update {target}: {exc}")
        return
    info(f"Config updated: {target}")


def apply_loaded_config(data: dict, expected_name: str = "") -> None:
    """Copy a loaded config into the module-level CONFIG used by the create flow."""
    if expected_name and data["name"] != expected_name:
        raise ConfigError(
            f"config describes sandbox '{data['name']}', not '{expected_name}'"
        )
    CONFIG.name = data["name"]
    CONFIG.path = data["project"]
    CONFIG.envfile = data["env_file"]
    CONFIG.secrets = data["scan_secrets"]
    CONFIG.secretsfile = data["secrets_settings"]
    CONFIG.volumes = list(data["volumes"])
    CONFIG.profiles = ",".join(data["profiles"])
    CONFIG.profile_directory = data["profile_directory"]


# --------------------------------------------------------------------------
# Secrets configuration
# --------------------------------------------------------------------------


def generate_secrets_file(filepath: str) -> None:
    content = json.dumps(
        {"search": list(DEFAULT_SEARCH_PATTERNS), "avoid": list(DEFAULT_AVOID_PATTERNS)},
        indent=2,
    )
    if DRY_RUN:
        info(f"  > generating {filepath}:")
        for line in content.splitlines():
            info(f"      {line}")
    else:
        Path(filepath).write_text(content + "\n")
    info(f"Generated default secrets config file: {filepath}")


def read_secrets_config(secretsfile: str):
    """Return (search_patterns, avoid_patterns) from the -sf JSON, or the defaults."""
    if secretsfile and Path(secretsfile).is_file():
        # Validate up front: a malformed file used to yield zero patterns, which
        # silently degraded into "not searching for secrets files". Fail loudly.
        try:
            data = json.loads(Path(secretsfile).read_text())
            valid = isinstance(data, dict) and all(
                isinstance(data.get(key, []), list) for key in ("search", "avoid")
            )
        except (OSError, ValueError):
            valid = False
        if not valid:
            die(
                f"secrets config file '{secretsfile}' must be a JSON object whose "
                "'search' and 'avoid' entries are arrays"
            )
        return list(data.get("search", [])), list(data.get("avoid", []))
    return list(DEFAULT_SEARCH_PATTERNS), list(DEFAULT_AVOID_PATTERNS)


def find_secret_files(root: str, search_patterns, avoid_patterns):
    """Every file under root whose *basename* matches a pattern, minus avoided trees.

    Mirrors `find "$root" -type f \\( -name p -o ... \\) -not -path '*/avoid/*'`:
    matches are case-sensitive globs on the file name, and any directory named in
    avoid_patterns is pruned along with everything under it.
    """
    avoid = set(avoid_patterns)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in avoid)
        for filename in sorted(filenames):
            if any(fnmatch.fnmatchcase(filename, str(p)) for p in search_patterns):
                found.append(os.path.join(dirpath, filename))
    return found


def check_project_secrets_files() -> None:
    search_patterns, avoid_patterns = read_secrets_config(secretsfile())

    if not search_patterns:
        info("Not searching for secrets files, let's continue")
        info()
        return

    info(f"Looking for secrets files in {path()}, please wait...")
    secrets_found = find_secret_files(path(), search_patterns, avoid_patterns)

    if secrets_found:
        for entry in secrets_found:
            info(entry)
        die(
            "secrets files detected within this project, make sure those can not be "
            "reached from sandbox and run this command again"
        )
    info("No secrets files detected within this project, let's continue")
    info()


def find_git_dir(root: str) -> str:
    """The project's .git directory, or "" when the project is not a repository.

    `find -print -quit` visits in an unspecified order; walking sorted makes the
    result deterministic when a project contains nested repositories.
    """
    if os.path.isdir(os.path.join(root, ".git")):
        return os.path.join(root, ".git")
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(dirnames)
        if ".git" in dirnames:
            return os.path.join(dirpath, ".git")
    return ""


def docker_daemon_running() -> bool:
    """Is the Docker daemon actually reachable?

    Having the `docker` CLI on PATH says nothing about the daemon. With Docker
    Desktop installed but not started, every `docker run` dies with
    "open //./pipe/docker_engine: The system cannot find the file specified" - and
    that used to be misreported as a gitleaks finding.
    """
    return run(["docker", "info"], check=False, capture=True).returncode == 0


def docker_daemon_start_lines():
    """How to start the Docker daemon on this host, ready to print as-is."""
    if os.name == "nt":
        return [
            "Start Docker Desktop from the Start menu, or from PowerShell:",
            '    Start-Process "$env:ProgramFiles\\Docker\\Docker\\Docker Desktop.exe"',
        ]
    if sys.platform == "darwin":
        return [
            "Start Docker Desktop, or from a terminal:",
            "    open -a Docker",
        ]
    return [
        "Start the docker service:",
        "    sudo systemctl start docker     # or: sudo service docker start",
    ]


def check_project_secrets_git() -> None:
    gitrepo = find_git_dir(path())

    if not gitrepo:
        info("GIT repository not found, not running leaks detection")
        info()
        return

    info(f"Detecting secrets within GIT repository {gitrepo}, please wait...")

    # `docker run` failing used to be indistinguishable from gitleaks finding a
    # leak, so a missing Docker reported "detected at least 1 leak" and looked
    # like a real finding. Say which it is.
    if not DRY_RUN and not have("docker"):
        die(
            "Docker is required for the git secret scan but was not found on PATH - "
            "install it (see docker-sbx-install.py) or re-run with '-s false' to skip "
            "the scan"
        )

    # Same reasoning one level down: the CLI being present does not mean the daemon
    # is up, and a stopped Docker Desktop would otherwise be reported as a leak.
    if not DRY_RUN and not docker_daemon_running():
        die(
            "\n       ".join(
                [
                    "Docker is installed but its daemon is not reachable, so the secret",
                    "scan cannot run. Start it, wait for it to finish starting, then run",
                    "this command again:",
                    *docker_daemon_start_lines(),
                    "Or re-run with '-s false' to skip the scan.",
                ]
            )
        )

    report_name = f"{name()}_gitleaks.json"
    # gitleaks exits 0 when the repository is clean and non-zero when it finds
    # something, so a non-zero here is the finding. (The bash original is
    # `if ! docker run ...`; the port had drifted to `if not <rc>`, which is the
    # opposite - it aborted on a clean repo and passed a leaking one.)
    if run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{path()}:/project",
            "-v",
            f"{gitrepo}:/repo",
            "-v",
            f"{os.getcwd()}:/report",
            GITLEAKS_IMAGE,
            "git",
            "/repo",
            "--gitleaks-ignore-path",
            "/project",
            "--report-path",
            f"/report/{report_name}",
        ],
        check=False,
    ).returncode:
        die(f"detected at least 1 leak in git repository {gitrepo}, fix it/them before running sandbox")
    info()


def check_project_secrets() -> None:
    if secrets().lower() != "false":
        banner(f"Checking project secrets {name()}")
        info()

        check_project_secrets_files()
        check_project_secrets_git()
    else:
        banner("Not checking project secrets, MAKE SURE NO SECRET CAN BE LEAKED THROUGH THIS PROJECT !!!")
        info()


# --------------------------------------------------------------------------
# Extra volumes
# --------------------------------------------------------------------------


def parse_volume_spec(spec: str):
    """Split "path[:ro|:rw]" into (hostpath, mode). Default mode is read-only."""
    low = spec.lower()
    mode = "ro"
    hostpath = spec
    if low.endswith(":ro"):
        mode = "ro"
        hostpath = spec[:-3]
    elif low.endswith(":rw"):
        mode = "rw"
        hostpath = spec[:-3]
    return hostpath, mode


def build_volume_args():
    volume_args = []
    for spec in volumes():
        hostpath, mode = parse_volume_spec(spec)

        # os.path.isabs, not the bash `[[ "$x" != /* ]]`: a Windows path such as
        # C:\\docs is absolute too.
        if not os.path.isabs(hostpath):
            die(f"volume path '{hostpath}' must be an absolute path")
        if not os.path.exists(hostpath):
            die(f"volume path '{hostpath}' does not exist")

        volume_args.append(f"{hostpath}:ro" if mode == "ro" else hostpath)
        info(f"Mounting {hostpath} (read-{'only' if mode == 'ro' else 'write'}) into sandbox {name()}")
    info()
    return volume_args


# --------------------------------------------------------------------------
# Sandbox creation and setup
# --------------------------------------------------------------------------


def parse_table_names(text: str):
    """First column of the human-readable `sbx ls` table, minus the header.

    Deliberately the same rule as docker-sbx-update-sandboxes.py, so the two
    scripts agree on what a sandbox name looks like.
    """
    headers = {"NAME", "ID", "SANDBOX", "AGENT"}
    names = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        first = fields[0]
        if first in headers or re.fullmatch(r"[-─=]+", first):
            continue
        names.append(first)
    return sorted(set(names))


def existing_sandbox_names():
    """Sandbox names, from `sbx ls -q`, falling back to the first table column."""
    proc = run(["sbx", "ls", "-q"], check=False, capture=True)
    if proc.returncode == 0:
        names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if names:
            return names
    return parse_table_names(run(["sbx", "ls"], check=False, capture=True).stdout)


def sandbox_ready() -> bool:
    """Has the sandbox finished its first start - runtime up, agent installed?

    Docker documents `sbx exec` as starting a stopped sandbox first, so this probe
    is also the noninteractive startup mechanism. The agent binary only
    exists once that first start has provisioned it, so testing for the binary is
    what actually tells us the boot is over. A non-login exec does not inherit
    `~/.local/bin` on PATH (the template's own startup hook exports it by hand), so
    the file is tested directly rather than with `command -v`.
    """
    probe = 'test -x "$HOME/.local/bin/claude" || test -x "$HOME/.claude/local/claude"'
    return (
        run(["sbx", "exec", "-i", name(), "sh", "-c", probe], check=False, quiet=True).returncode
        == 0
    )


def wait_for_sandbox() -> bool:
    """Wait for the new sandbox's first start to settle. False on timeout.

    The first start is when the guest gets its agent and when the sandbox's own
    startup hooks run apt; configuring a sandbox in the middle of that is what used
    to leave sandboxes that could not be started again.
    """
    if DRY_RUN:
        return True

    info(f"Waiting for sandbox {name()} to finish starting (up to {SANDBOX_READY_TIMEOUT}s)...")
    started = time.monotonic()
    reported = 0
    while True:
        if sandbox_ready():
            info(f"  sandbox {name()} is up")
            return True
        elapsed = int(time.monotonic() - started)
        if elapsed >= SANDBOX_READY_TIMEOUT:
            return False
        if elapsed // SANDBOX_READY_REPORT > reported:
            reported = elapsed // SANDBOX_READY_REPORT
            info(f"  still starting... ({elapsed}s)")
        time.sleep(SANDBOX_READY_POLL)


def create_sandbox(volume_args) -> None:
    banner(f"Creating sandbox {name()}")
    info()

    # Never reconfigure a sandbox we did not just create. `sbx create` fails on an
    # existing name, and that failure used to scroll past (sbx() runs with
    # check=False), so the setup then ran against the *previous* sandbox of that
    # name and the run still reported success.
    if DRY_RUN:
        # The listing cannot be queried in dry-run, but --force is an explicit
        # statement that the existing sandbox would be removed, so show it: a dry
        # run is supposed to print every command that would run.
        if CONFIG.recreate and CONFIG.force:
            sbx(["rm", name()])
    elif name() in existing_sandbox_names():
        if CONFIG.recreate and CONFIG.force:
            info(f"Removing existing sandbox {name()} (--force)")
            sbx(["rm", name()], check=True)
        elif CONFIG.recreate:
            die(
                f"sandbox '{name()}' already exists - re-run with `--force` to "
                "remove and recreate it"
            )
        else:
            die(
                f"sandbox '{name()}' already exists - remove it with `sbx rm {name()}`, "
                "or choose another name with -n"
            )

    # Fatal: every later step (cp, exec, policy) assumes this sandbox exists, and is
    # the one we just made.
    sbx(["create", "--name", name(), "claude", path(), *volume_args], check=True)


def setup_sandbox() -> None:
    banner(f"Updating sandbox {name()}")
    info()

    # Docker documents `sbx exec` as starting a stopped sandbox before running the
    # command. sandbox_ready() uses `sbx exec`, so its probes both trigger startup
    # when needed and wait for first-start agent provisioning to finish. No separate
    # start command (or interactive `sbx run`) is needed before configuring it.
    if not wait_for_sandbox():
        die(
            f"sandbox '{name()}' did not start within {SANDBOX_READY_TIMEOUT}s, so it was "
            "left unconfigured.\n"
            f"       Check the runtime (sbx diagnose), then re-run this command once "
            f"`sbx run {name()}` works."
        )

    setup_script = script_dir() / "sandbox" / "docker-sbx-setup-sandbox.sh"
    slack_script = script_dir() / "sandbox" / "docker-sbx-slack-notify.sh"
    nctalk_script = script_dir() / "sandbox" / "docker-sbx-nctalk-notify.sh"

    sbx(["cp", str(setup_script), f"{name()}:{SBX_HOME}/setup-sandbox.sh"])
    sbx(["exec", "-ti", name(), "sudo", "chmod", "755", f"{SBX_HOME}/setup-sandbox.sh"])

    # Each provider's hook script is staged only when that feature is switched on,
    # so a sandbox without it keeps exactly the previous behaviour. They are
    # independent: one, the other, or both may be enabled together.
    if slack_enabled():
        sbx(["cp", str(slack_script), f"{name()}:{SBX_HOME}/slack-notify.sh"])
        sbx(["exec", "-ti", name(), "sudo", "chmod", "755", f"{SBX_HOME}/slack-notify.sh"])

    if nctalk_enabled():
        sbx(["cp", str(nctalk_script), f"{name()}:{SBX_HOME}/nctalk-notify.sh"])
        sbx(["exec", "-ti", name(), "sudo", "chmod", "755", f"{SBX_HOME}/nctalk-notify.sh"])

    # The in-sandbox script takes the project path and the env-file path, with "-"
    # standing in for "no env file": an empty argument does not survive the trip
    # through `sbx exec`.
    if not envfile():
        sbx(["exec", "-ti", name(), "bash", f"{SBX_HOME}/setup-sandbox.sh", path(), "-"])
        return

    info(f"Using env file {envfile()}")
    envbasename = os.path.basename(envfile())
    sbx(["cp", envfile(), f"{name()}:{SBX_HOME}/{envbasename}"])
    sbx(["exec", "-ti", name(), "sudo", "chmod", "644", f"{SBX_HOME}/{envbasename}"])
    sbx(
        [
            "exec", "-ti", name(), "bash", f"{SBX_HOME}/setup-sandbox.sh",
            path(), f"{SBX_HOME}/{envbasename}",
        ]
    )


# --------------------------------------------------------------------------
# Per-sandbox network policy
# --------------------------------------------------------------------------


def host_from_url(url: str) -> str:
    """Strip the scheme, then everything from the first ':' or '/'."""
    stripped = re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE)
    return re.split(r"[:/]", stripped, maxsplit=1)[0]


def update_sbx_policy() -> None:
    if not envfile() or not Path(envfile()).is_file():
        return

    base_url = env_file_get("ANTHROPIC_BASE_URL")
    api_key = env_file_get("ANTHROPIC_API_KEY")

    if base_url:  # LLM server hostname is provided
        allowed_host = host_from_url(base_url)
        info()
        info(f"Allowing {allowed_host} in sandbox {name()} network policy")
        sbx(["policy", "allow", "network", "--sandbox", name(), allowed_host])
    elif api_key:  # Using Anthropic API key
        info()
        info(f"Allowing api.anthropic.com in sandbox {name()} network policy")
        sbx(["policy", "allow", "network", "--sandbox", name(), "api.anthropic.com"])
    else:  # Using Anthropic regular login method
        info()
        info(f"Allowing claude.ai and platform.claude.com in sandbox {name()} network policy")
        sbx(["policy", "allow", "network", "--sandbox", name(), "claude.ai"])
        sbx(["policy", "allow", "network", "--sandbox", name(), "platform.claude.com"])


# --------------------------------------------------------------------------
# Optional Slack notifications (host side)
# --------------------------------------------------------------------------


def unquote(value: str) -> str:
    """Trim surrounding whitespace, then one pair of quote characters.

    Mirrors the bash sed chain, which applies each substitution independently:
    strip space, strip a leading/"," then a leading/trailing '.
    """
    value = value.strip()
    if value.startswith('"'):
        value = value[1:]
    if value.endswith('"'):
        value = value[:-1]
    if value.startswith("'"):
        value = value[1:]
    if value.endswith("'"):
        value = value[:-1]
    return value


def env_file_get(key: str) -> str:
    """Read KEY from the -e env file without sourcing it.

    Matches the bash `grep "^KEY=" | tail -1 | cut -d= -f2- | sed ...`: the last
    matching line wins, and the value is unquoted. An absent key, or no env file,
    yields "" rather than an error.
    """
    filepath = envfile()
    if not filepath or not Path(filepath).is_file():
        return ""
    found = ""
    try:
        lines = Path(filepath).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith(f"{key}="):
            found = line.split("=", 1)[1]
    return unquote(found)


def slack_enabled() -> bool:
    return env_file_get("SBX_SLACK_ENABLED") == "true"


def slack_events() -> str:
    events = env_file_get("SBX_SLACK_EVENTS")
    return events if events else DEFAULT_SLACK_EVENTS


def nctalk_enabled() -> bool:
    return env_file_get("SBX_NCTALK_ENABLED") == "true"


def nctalk_events() -> str:
    events = env_file_get("SBX_NCTALK_EVENTS")
    return events if events else DEFAULT_NCTALK_EVENTS


def validate_notify_events(events: str, varname: str, supported) -> None:
    """Abort on an event name outside the supported set, naming the variable."""
    for event in events.split(","):
        event = event.replace(" ", "")
        if not event:
            continue
        if event not in supported:
            info()
            die(
                f"unsupported {varname} entry '{event}'\n"
                "       Supported events: " + ", ".join(supported)
            )


def validate_slack_config() -> None:
    if not slack_enabled():
        return

    webhook = env_file_get("SBX_SLACK_WEBHOOK_URL")
    token = env_file_get("SBX_SLACK_BOT_TOKEN")
    channel = env_file_get("SBX_SLACK_CHANNEL")

    if not webhook and (not token or not channel):
        info()
        die(
            "SBX_SLACK_ENABLED is true but no usable Slack target is configured.\n"
            "       Set SBX_SLACK_WEBHOOK_URL, or SBX_SLACK_BOT_TOKEN together with SBX_SLACK_CHANNEL."
        )

    validate_notify_events(slack_events(), "SBX_SLACK_EVENTS", SUPPORTED_SLACK_EVENTS)


def validate_nctalk_config() -> None:
    if not nctalk_enabled():
        return

    missing = [
        key
        for key in ("SBX_NCTALK_URL", "SBX_NCTALK_USER", "SBX_NCTALK_TOKEN", "SBX_NCTALK_ROOM")
        if not env_file_get(key)
    ]
    if missing:
        info()
        die(
            "SBX_NCTALK_ENABLED is true but no usable Nextcloud Talk target is configured.\n"
            "       Set " + ", ".join(missing) + "."
        )

    url = env_file_get("SBX_NCTALK_URL")
    try:
        parsed = urlsplit(url)
        # Accessing port validates it; urlsplit otherwise accepts values such as
        # 'https://host:not-a-port', which curl would reject only at event time.
        parsed.port
        valid_url = (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.hostname)
            and not any(char.isspace() for char in url)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        info()
        die("SBX_NCTALK_URL must be an absolute http:// or https:// URL with a valid host.")

    validate_notify_events(nctalk_events(), "SBX_NCTALK_EVENTS", SUPPORTED_NCTALK_EVENTS)


def update_sbx_policy_slack() -> None:
    if not slack_enabled():
        return

    webhook = env_file_get("SBX_SLACK_WEBHOOK_URL")
    token = env_file_get("SBX_SLACK_BOT_TOKEN")

    # Allow only what the configured credential style actually needs. Without this
    # the sandbox's deny-by-default policy would silently drop every notification.
    hosts = []
    if webhook:
        hosts.append(host_from_url(webhook) or "hooks.slack.com")
    if token:
        hosts.append("slack.com")

    if not hosts:
        return

    info()
    info(f"Allowing Slack endpoints in sandbox {name()} network policy")
    for host in hosts:
        info(f"  allowing {host}")
        sbx(["policy", "allow", "network", "--sandbox", name(), host])


def update_sbx_policy_nctalk() -> None:
    if not nctalk_enabled():
        return

    # The whole Nextcloud instance is served from one host, so allowing it covers
    # the Talk endpoint. Without this the sandbox's deny-by-default policy would
    # silently drop every notification.
    host = host_from_url(env_file_get("SBX_NCTALK_URL"))
    if not host:
        return

    info()
    info(f"Allowing Nextcloud Talk endpoint in sandbox {name()} network policy")
    info(f"  allowing {host}")
    sbx(["policy", "allow", "network", "--sandbox", name(), host])


# --------------------------------------------------------------------------
# Policy profiles
# --------------------------------------------------------------------------


class ProfileError(Exception):
    """A profile that cannot be parsed or is not shaped as a per-sandbox policy."""


def _strip_value_comment(value: str) -> str:
    marker = value.find(" #")
    return value[:marker].rstrip() if marker >= 0 else value


def _scalar(value: str, where: str) -> str:
    """A plain or quoted scalar. Anything more exotic is refused, not guessed."""
    value = value.strip()
    if value[:1] in ("&", "*", "[", "{", "|", ">", "%", "@", "`"):
        raise ProfileError(f"{where}: unsupported YAML construct: {value!r}")
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        # '' is YAML's escape for a single quote inside a single-quoted scalar.
        # Config paths (Windows usernames, for one) can contain apostrophes, and
        # the writer emits them this way.
        while quote == "'" and end != -1 and value[end : end + 2] == "''":
            end = value.find(quote, end + 2)
        if end != -1:
            remainder = value[end + 1 :].strip()
            # A trailing comment is allowed after the closing quote; anything else
            # is not a scalar we understand. Testing the last character (as an
            # earlier version did) left the quotes in place whenever a comment
            # followed, so a quoted domain was used verbatim, quotes included.
            if not remainder or remainder.startswith("#"):
                inner = value[1:end]
                return inner.replace("''", "'") if quote == "'" else inner
    return _strip_value_comment(value).strip()


def _parse_block(lines, index, indent, where):
    if lines[index][0] != indent:
        raise ProfileError(f"{where}: inconsistent indentation")
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent, where)
    return _parse_mapping(lines, index, indent, where)


def _parse_sequence(lines, index, indent, where):
    items = []
    while index < len(lines):
        level, text = lines[index]
        if level < indent:
            break
        if level > indent:
            raise ProfileError(f"{where}: unexpected indentation in a list")
        if not text.startswith("- "):
            break
        items.append(_scalar(text[2:], where))
        index += 1
    return items, index


def _parse_mapping(lines, index, indent, where):
    mapping = {}
    while index < len(lines):
        level, text = lines[index]
        if level < indent:
            break
        if level > indent:
            raise ProfileError(f"{where}: unexpected indentation")
        if text.startswith("- "):
            break
        if ":" not in text:
            raise ProfileError(f"{where}: expected 'key: value', found {text!r}")
        key, _, rest = text.partition(":")
        key = _scalar(key.strip(), where)
        rest = rest.strip()
        index += 1
        if rest:
            mapping[key] = _scalar(rest, where)
        elif index < len(lines) and lines[index][0] > indent:
            mapping[key], index = _parse_block(lines, index, lines[index][0], where)
        else:
            mapping[key] = None
    return mapping, index


def parse_profile_minimal(text: str, where: str):
    """Parse the documented profile subset: nested mappings, '- ' lists, scalars.

    Deliberately narrow. Anchors, flow collections, block scalars and tabs are
    rejected with a clear message rather than guessed at; PyYAML, when it is
    installed, is used instead of this.
    """
    lines = []
    for raw in text.splitlines():
        leading = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in leading:
            raise ProfileError(f"{where}: tab characters are not supported for indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))

    if not lines:
        return {}

    value, index = _parse_block(lines, 0, lines[0][0], where)
    if index != len(lines):
        raise ProfileError(f"{where}: could not parse the whole file")
    return value


def validate_profile(data, where: str):
    """Validate the documented profile schema, returning (allowed, denied).

    Unknown keys are refused at every level rather than ignored. A silent drop is
    the dangerous case: `policy:` (a typo for `policies:`) or `allow_domains:`
    would otherwise produce a sandbox that looks configured but is not.
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProfileError(f"profile {where} must be a YAML mapping")

    for key in data:
        if not isinstance(key, str):
            raise ProfileError(f"profile {where} has a non-string top-level key")
        if key.lower() in FORBIDDEN_PROFILE_KEYS:
            raise ProfileError(f"profile {where} contains forbidden global key '{key}'")
        if key not in PROFILE_KEYS:
            raise ProfileError(
                f"profile {where} has unknown top-level key '{key}' "
                f"(allowed: {', '.join(PROFILE_KEYS)})"
            )

    for key in ("name", "description"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ProfileError(f"profile {where} '{key}' must be a string")

    policies = data.get("policies")
    if policies is None:
        policies = {}
    if not isinstance(policies, dict):
        raise ProfileError(f"profile {where} 'policies' must be a mapping")

    network = None
    for section, value in policies.items():
        if not isinstance(section, str):
            raise ProfileError(f"profile {where} has a non-string policy section key")
        if section.lower() != "network":
            raise ProfileError(
                f"profile {where} has unsupported policy section '{section}' (only 'network' is supported)"
            )
        network = value

    if network is None:
        network = {}
    if not isinstance(network, dict):
        raise ProfileError(f"profile {where} 'policies.network' must be a mapping")

    actions = {}
    for action, value in network.items():
        if not isinstance(action, str) or action.lower() not in ("allow", "deny"):
            raise ProfileError(
                f"profile {where} 'policies.network' has unknown key '{action}' "
                "(allowed: allow, deny)"
            )
        actions[action.lower()] = value

    allowed, denied = [], []
    for action in ("allow", "deny"):
        entries = actions.get(action)
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ProfileError(f"profile {where} 'policies.network.{action}' must be a list")
        for entry in entries:
            if not isinstance(entry, str):
                raise ProfileError(
                    f"profile {where} 'policies.network.{action}' contains a non-string entry"
                )
            domain = entry.strip()
            if not domain:
                raise ProfileError(
                    f"profile {where} 'policies.network.{action}' contains an empty entry"
                )
            if ".." in domain or not PROFILE_DOMAIN_RE.match(domain):
                raise ProfileError(
                    f"profile {where} 'policies.network.{action}' contains invalid domain "
                    f"'{domain}' (expected a host such as example.com or *.example.com)"
                )
            (allowed if action == "allow" else denied).append(domain)
    return allowed, denied


def load_profile(profile_path: str):
    """(allowed, denied) for one profile file, using PyYAML when it is available."""
    where = profile_path
    try:
        text = Path(profile_path).read_text()
    except OSError as exc:
        raise ProfileError(f"failed to read profile {where}: {exc}")

    try:
        import yaml  # noqa: F401  (optional: full YAML when installed)
    except ImportError:
        data = parse_profile_minimal(text, f"profile {where}")
    else:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ProfileError(f"failed to parse profile {where}: {exc}")
    return validate_profile(data, where)


class ProfileSource:
    """A profile definition plus the directory that carries its assets, if any."""

    def __init__(self, name: str, definition: Path, assets_root=None) -> None:
        self.name = name
        self.definition = definition
        self.assets_root = assets_root


def resolve_profile(profiles_dir: Path, profile_name: str) -> ProfileSource:
    """Find <dir>/<name>/profile.yaml or <dir>/<name>.yaml|.yml.

    A directory form is self-contained: its definition and its asset tree live
    together. Defining the same profile twice is ambiguous, so it is an error
    rather than a silent precedence rule.
    """
    if (
        not profile_name
        or profile_name in (".", "..")
        or any(char in profile_name for char in ("/", "\\"))
    ):
        raise ProfileError(f"invalid profile name '{profile_name}'")
    directory = profiles_dir / profile_name
    definitions = [
        directory / candidate
        for candidate in PROFILE_DEFINITION_NAMES
        if (directory / candidate).is_file()
    ]
    flat = [
        profiles_dir / f"{profile_name}.{extension}"
        for extension in ("yaml", "yml")
        if (profiles_dir / f"{profile_name}.{extension}").is_file()
    ]

    if definitions and flat:
        raise ProfileError(
            f"profile '{profile_name}' is defined both as a directory and as "
            f"'{flat[0].name}' in '{profiles_dir}'"
        )
    if definitions:
        if len(definitions) > 1:
            raise ProfileError(
                f"profile '{profile_name}' has both profile.yaml and profile.yml "
                f"in '{directory}'"
            )
        return ProfileSource(profile_name, definitions[0], directory)
    if flat:
        if len(flat) > 1:
            raise ProfileError(
                f"profile '{profile_name}' exists as both {flat[0].name} and "
                f"{flat[1].name} in '{profiles_dir}'"
            )
        return ProfileSource(profile_name, flat[0])
    raise ProfileError(f"profile '{profile_name}' not found in '{profiles_dir}'")


def _collect_asset_tree(root: Path, prefix: str = ""):
    """(arcname, source, mode, size) for every regular file under root, sorted.

    Symlinks and special files are refused: an asset bundle is copied into the
    project volume or the agent's home, and a link could point outside either.
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for directory in list(dirnames):
            candidate = Path(dirpath) / directory
            if candidate.is_symlink():
                raise ProfileError(
                    f"profile asset '{candidate}' is a symlink; only regular files "
                    "and directories are allowed"
                )
        dirnames.sort()
        for filename in sorted(filenames):
            candidate = Path(dirpath) / filename
            try:
                status = candidate.lstat()
            except OSError as exc:
                raise ProfileError(f"cannot read profile asset '{candidate}': {exc}")
            if not stat.S_ISREG(status.st_mode):
                raise ProfileError(
                    f"profile asset '{candidate}' is not a regular file"
                )
            relative = candidate.relative_to(root).as_posix()
            arcname = f"{prefix}/{relative}" if prefix else relative
            files.append((arcname, candidate, status.st_mode & 0o777, status.st_size))
    return files


def _check_asset_paths(level: str, files) -> None:
    """Refuse files whose first component is owned by docker-sbx or Claude Code."""
    denied = AGENT_ASSET_DENY if level == "agent" else PROJECT_ASSET_DENY
    for arcname, _source, _mode, _size in files:
        first = arcname.split("/", 1)[0]
        if first in denied:
            raise ProfileError(
                f"profile asset '{arcname}' may not be installed at {level} level: "
                f"'{first}' is managed by docker-sbx or Claude Code"
            )


def profile_asset_plan(assets_root: Path):
    """Split a profile's asset tree into (agent-level, project-level) file lists.

    `agent/` and `project/` are explicit targets; every other root entry mirrors
    ~/.claude, which is the default level.
    """
    agent, project = [], []
    for entry in sorted(assets_root.iterdir(), key=lambda item: item.name):
        if entry.name in PROFILE_DEFINITION_NAMES:
            continue
        if entry.is_symlink():
            raise ProfileError(
                f"profile asset '{entry}' is a symlink; only regular files and "
                "directories are allowed"
            )
        if entry.name in ("agent", "project"):
            if not entry.is_dir():
                raise ProfileError(
                    f"profile entry '{entry.name}' must be a directory"
                )
            target = agent if entry.name == "agent" else project
            target.extend(_collect_asset_tree(entry))
        elif entry.is_dir():
            agent.extend(_collect_asset_tree(entry, prefix=entry.name))
        elif entry.is_file():
            status = entry.lstat()
            agent.append((entry.name, entry, status.st_mode & 0o777, status.st_size))
        else:
            raise ProfileError(f"profile entry '{entry}' is not a regular file or directory")

    count = len(agent) + len(project)
    total = sum(entry[3] for entry in agent + project)
    if count > ASSET_MAX_FILES or total > ASSET_MAX_BYTES:
        raise ProfileError(
            f"profile assets are too large to install ({count} files, {total} bytes); "
            f"the limit is {ASSET_MAX_FILES} files and {ASSET_MAX_BYTES} bytes"
        )
    _check_asset_paths("agent", agent)
    _check_asset_paths("project", project)
    return agent, project


def build_asset_tarball(files, destination: Path) -> None:
    """Package the selected files reproducibly; no external tar on the host."""
    with tarfile.open(destination, "w:gz") as archive:
        for arcname, source, mode, _size in files:
            info = archive.gettarinfo(str(source), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = mode
            with open(source, "rb") as handle:
                archive.addfile(info, handle)


def sandbox_project_path(host_path: str) -> str:
    """The project path as the sandbox sees it (Windows C:\\x becomes /c/x)."""
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", host_path)
    if match:
        return "/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/")
    return host_path


def _push_asset_tree(level: str, files, workspace: Path, target: str, display: str, guard: str = "") -> None:
    """Tar one target's files, copy the archive in, and unpack it there."""
    archive = workspace / f"{level}.tgz"
    build_asset_tarball(files, archive)

    for arcname, _source, _mode, _size in files:
        info(f"  installing {arcname} -> {display}/{arcname}")

    guest_path = f"{SBX_HOME}/.docker-sbx-assets-{os.getpid()}-{level}.tgz"
    sbx(["cp", str(archive), f"{name()}:{guest_path}"], check=True)

    quoted_guest = shlex.quote(guest_path)
    command = " && ".join(
        [
            "command -v tar >/dev/null 2>&1 || "
            "{ echo 'tar is required in the sandbox to install profile assets' >&2; exit 1; }",
            *([guard] if guard else []),
            f"mkdir -p {target}",
            f"tar -xzf {quoted_guest} -C {target}",
            f"rm -f {quoted_guest}",
        ]
    )
    sbx(["exec", "-i", name(), "sh", "-c", command], check=True)


def install_profile_assets(profile_name: str, assets_root: Path) -> None:
    """Copy a profile's asset tree into the sandbox, at agent and/or project level."""
    try:
        agent_files, project_files = profile_asset_plan(assets_root)
        if not agent_files and not project_files:
            return
        if project_files and not path():
            raise ProfileError(
                f"profile '{profile_name}' installs project-level assets, but sandbox "
                f"'{name()}' has no saved config recording its project path - apply the "
                "profile when the sandbox is created, or recreate it from a config"
            )

        with tempfile.TemporaryDirectory(prefix="docker-sbx-assets-") as workspace:
            if agent_files:
                _push_asset_tree(
                    "agent", agent_files, Path(workspace), '"$HOME/.claude"', "~/.claude"
                )
            if project_files:
                project_path = sandbox_project_path(path())
                quoted_project = shlex.quote(project_path)
                message = shlex.quote(
                    f"project path {project_path} is not visible inside the sandbox"
                )
                _push_asset_tree(
                    "project",
                    project_files,
                    Path(workspace),
                    shlex.quote(f"{project_path}/.claude"),
                    f"{project_path}/.claude",
                    guard=f"test -d {quoted_project} || {{ echo {message} >&2; exit 1; }}",
                )
    except (OSError, tarfile.TarError) as exc:
        raise ProfileError(
            f"failed to package the assets of profile '{profile_name}': {exc}"
        )


def split_profile_names(raw: str):
    """The -profile list, trimmed, without blanks or duplicates, order preserved."""
    names = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in names:
            names.append(item)
    return names


def load_profile_sources():
    """Resolve, parse and asset-check every selected profile, applying nothing.

    Doing this before `sbx create` means a typo'd profile name or an invalid bundle
    aborts while nothing exists yet, instead of leaving a created sandbox behind
    that has no saved config and has to be removed by hand.
    """
    names = split_profile_names(profiles())
    if not names:
        return []
    profiles_dir = Path(profile_directory()) if profile_directory() else script_dir() / "profiles"
    if not profiles_dir.is_dir():
        die(f"profile directory '{profiles_dir}' does not exist")

    sources = []
    for profile_name in names:
        try:
            source = resolve_profile(profiles_dir, profile_name)
            allowed, denied = load_profile(str(source.definition))
            if source.assets_root is not None:
                profile_asset_plan(source.assets_root)
        except ProfileError as exc:
            die(str(exc))
        sources.append((source, allowed, denied))
    return sources


def validate_profiles() -> None:
    """Fail on a bad profile before a sandbox is created or touched."""
    load_profile_sources()


def apply_profiles() -> None:
    if not profiles():
        return

    sources = load_profile_sources()
    if not sources:
        return

    banner(f"Applying profiles to {name()}")
    info()

    for source, allowed, denied in sources:
        info(f"Loading profile: {source.name}")

        for domain in allowed:
            info(f"  allowing {domain}")
            sbx(["policy", "allow", "network", "--sandbox", name(), domain])
        for domain in denied:
            info(f"  denying {domain}")
            sbx(["policy", "deny", "network", "--sandbox", name(), domain])

        if source.assets_root is not None:
            try:
                install_profile_assets(source.name, source.assets_root)
            except ProfileError as exc:
                die(str(exc))

    info()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


# The parsed configuration, set once in main() and read by the functions that
# mirror the bash globals ($name, $path, $envfile, ...).
CONFIG = Config()


def name() -> str:
    return CONFIG.name


def path() -> str:
    return CONFIG.path


def envfile() -> str:
    return CONFIG.envfile


def secrets() -> str:
    return CONFIG.secrets


def secretsfile() -> str:
    return CONFIG.secretsfile


def volumes():
    return CONFIG.volumes


def profiles() -> str:
    return CONFIG.profiles


def profile_directory() -> str:
    return CONFIG.profile_directory


def main_update(argv=None) -> int:
    """Entry point for docker-sbx-apply-profile.py: profiles -> live sandbox."""
    global DRY_RUN, CONFIG

    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        CONFIG = parse_args(argv)
    except CliError as exc:
        print(exc)
        usage(1, "update")

    DRY_RUN = CONFIG.dry_run

    if CONFIG.show_help:
        usage(0, "update")

    try:
        validate_mode_flags(CONFIG, "update")
    except CliError as exc:
        print(exc)
        usage(1, "update")

    if DRY_RUN:
        banner("docker-sbx profile update (dry-run)")
        info("dry-run: nothing will be changed")

    if not name():
        banner("Missing parameter, you must provide the sandbox name with -n")
        usage(1, "update")
    if not profiles():
        die("-profile is required: a comma-separated list of profile names")

    if not DRY_RUN and not have("sbx"):
        die(
            "the sbx command was not found on PATH - run docker-sbx-install.py first, "
            "then open a new terminal so the PATH change is picked up"
        )

    # A profile may install project-level assets, and its profile directory may
    # have been recorded at creation. Both live in the saved config; without it,
    # fall back to the CLI/defaults (and refuse project assets later, rather than
    # writing to "/.claude" because the project path is unknown).
    try:
        saved = config_file(name())
    except ConfigError as exc:
        saved = None
        warn(str(exc))
    if saved is not None and saved.is_file():
        try:
            data = load_config(str(saved))
        except ConfigError as exc:
            warn(
                f"could not read {saved}: {exc}; its project path and profile "
                "directory are unavailable"
            )
        else:
            CONFIG.path = data["project"]
            if not CONFIG.profile_directory and data["profile_directory"]:
                CONFIG.profile_directory = data["profile_directory"]

    # Validate everything before changing the sandbox: a later profile must not
    # fail after an earlier one's rules and files were already applied.
    validate_profiles()

    if not DRY_RUN:
        # existing_sandbox_names() answers [] both for an empty list and for a
        # failed query, so ask whether sbx can be queried at all before blaming
        # the sandbox for missing.
        if run(["sbx", "version"], check=False, capture=True, quiet=True).returncode != 0:
            die(
                "could not query sbx - check that the Docker runtime is running "
                "(sbx diagnose), then re-run this command"
            )
        if name() not in existing_sandbox_names():
            die(
                f"sandbox '{name()}' does not exist - create it with "
                f"{Path(sys.argv[0]).name} -n {name()} -p <project path>"
            )

    # A stopped sandbox is started by the first `sbx exec`; wait for that first
    # start (and its agent provisioning) to settle before copying anything in.
    if not wait_for_sandbox():
        die(
            f"sandbox '{name()}' did not start within {SANDBOX_READY_TIMEOUT}s, so "
            "profiles were not applied.\n"
            f"       Check the runtime (sbx diagnose), then re-run this command once "
            f"`sbx run {name()}` works."
        )

    apply_profiles()
    update_saved_config(split_profile_names(profiles()))
    return 0


def main(argv=None) -> int:
    global DRY_RUN, CONFIG

    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        CONFIG = parse_args(argv)
    except CliError as exc:
        print(exc)
        usage(1)

    DRY_RUN = CONFIG.dry_run

    if CONFIG.show_help:
        usage(0)

    try:
        mode = select_mode(CONFIG)
    except CliError as exc:
        print(exc)
        usage(1)

    if DRY_RUN:
        banner("docker-sbx sandbox creation (dry-run)")
        info("dry-run: nothing will be changed")

    # -gsf writes a template and exits, before any other validation - including
    # the per-mode flag checks below, which is the precedence it has always had.
    if CONFIG.gensecretsfile:
        directory = os.path.dirname(CONFIG.gensecretsfile)
        if directory and not os.path.isdir(directory):
            die(f"directory '{directory}' does not exist")
        generate_secrets_file(CONFIG.gensecretsfile)
        return 0

    try:
        validate_mode_flags(CONFIG, mode)
    except CliError as exc:
        print(exc)
        usage(1)

    if mode == "from-config":
        try:
            apply_loaded_config(load_config(CONFIG.from_config))
        except ConfigError as exc:
            die(str(exc))
    elif mode == "recreate":
        if not CONFIG.name:
            banner("Missing parameter, you must provide the sandbox name with -n")
            usage(1)
        try:
            target = config_file(CONFIG.name)
        except ConfigError as exc:
            die(str(exc))
        if not target.is_file():
            die(
                f"no saved config for sandbox '{CONFIG.name}' at {target} - create "
                f"the sandbox with -n/-p first, or pass --config-dir"
            )
        try:
            apply_loaded_config(load_config(str(target)), expected_name=CONFIG.name)
        except ConfigError as exc:
            die(str(exc))

    # A config records the env file by path. If it has moved (or the project was
    # rebuilt on another machine), say so instead of creating a sandbox that
    # silently lacks the credentials the config promises.
    if (
        mode in ("recreate", "from-config")
        and CONFIG.envfile
        and not os.path.isfile(CONFIG.envfile)
    ):
        die(f"env file '{CONFIG.envfile}' recorded in the config was not found")

    if CONFIG.secretsfile and not os.path.isfile(CONFIG.secretsfile):
        die(f"secrets config file '{CONFIG.secretsfile}' not found")

    if not CONFIG.name or not CONFIG.path:
        banner("Missing parameter, you must at least provide a sandbox name and a project path")
        usage(1)

    if not os.path.isabs(CONFIG.path):
        die(f"project path '{CONFIG.path}' must be an absolute path")

    if not os.path.isdir(CONFIG.path):
        die(f"project path '{CONFIG.path}' does not exist or is not a directory")

    if CONFIG.profile_directory and not os.path.isdir(CONFIG.profile_directory):
        die(f"profile directory '{CONFIG.profile_directory}' does not exist")

    # The arguments the user passed are checked before the environment they are
    # passed in: the bash implementation has no `sbx` precondition, so validating
    # Slack after it made the two report different errors on a host without `sbx`.
    validate_slack_config()
    validate_nctalk_config()

    # Fail fast, before the secret scan and before anything is created. Individual
    # `sbx` calls are deliberately non-fatal (sbx() runs with check=False, as the
    # bash version did), so without this a missing sbx would sail through every step
    # doing nothing and still report success.
    if not DRY_RUN and not have("sbx"):
        die(
            "the sbx command was not found on PATH - run docker-sbx-install.py first, "
            "then open a new terminal so the PATH change is picked up"
        )

    # Resolve and validate profiles before anything is created: a typo'd name or an
    # invalid bundle used to fail after `sbx create`, leaving a sandbox behind that
    # had to be removed by hand.
    validate_profiles()

    volume_args = build_volume_args()
    check_project_secrets()
    create_sandbox(volume_args)
    setup_sandbox()
    update_sbx_policy()
    update_sbx_policy_slack()
    update_sbx_policy_nctalk()
    apply_profiles()
    saved_config = save_created_config()

    info()
    banner(
        f"Sandbox {name()} {'would be' if DRY_RUN else 'is'} ready"
    )
    if saved_config:
        info(f"Config saved: {saved_config}")
        info(f"Recreate later with: {Path(sys.argv[0]).name} -n {name()} --recreate")
    info()
    return 0


if __name__ == "__main__":
    sys.exit(main())
