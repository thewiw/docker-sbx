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
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

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

# A profile may never imply a global policy - it is always per-sandbox.
FORBIDDEN_PROFILE_KEYS = ("global", "default", "all_sandboxes", "sandbox_default")

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
        self.dry_run = False
        self.show_help = False


def usage_text() -> str:
    scriptname = Path(sys.argv[0]).name
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
            "  --dry-run     Print every command that would run, and change nothing",
            "  -h, --help    Show this help and exit",
            "",
            "Example(s):",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -e sbx.env",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -e sbx.env -sf secrets.json",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -v /mnt/c/docs -v /mnt/c/data:rw",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -profile python",
            f"  from WSL : {scriptname} -n test -p /mnt/c/Projects/test -profile python,java",
            f"  from WSL : {scriptname} -gsf secrets.json",
            "",
        ]
    )


def usage(exit_code: int = 1) -> "None":
    print(usage_text())
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
        if arg == "--dry-run":
            cfg.dry_run = True
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
    return cfg


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

    `sbx exec` cannot succeed before the runtime is up, and the agent binary only
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
    if not DRY_RUN and name() in existing_sandbox_names():
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

    # Configuring the sandbox means running commands inside it, and that is also
    # what starts it - so wait for the first start to finish before touching it.
    # `sbx start` is not fatal: if the runtime refuses to come up, its own message
    # is more useful than ours, and the readiness probe below is what decides.
    sbx(["start", name()])
    if not wait_for_sandbox():
        die(
            f"sandbox '{name()}' did not start within {SANDBOX_READY_TIMEOUT}s, so it was "
            "left unconfigured.\n"
            f"       Find out why it will not start (sbx diagnose, then `sbx start {name()}`), "
            f"and re-run this command once `sbx run {name()}` works."
        )

    setup_script = script_dir() / "sandbox" / "docker-sbx-setup-sandbox.sh"
    slack_script = script_dir() / "sandbox" / "docker-sbx-slack-notify.sh"

    sbx(["cp", str(setup_script), f"{name()}:{SBX_HOME}/setup-sandbox.sh"])
    sbx(["exec", "-ti", name(), "sudo", "chmod", "755", f"{SBX_HOME}/setup-sandbox.sh"])

    # The Slack hook script is staged only when the feature is switched on, so a
    # sandbox without it keeps exactly the previous behaviour.
    if slack_enabled():
        sbx(["cp", str(slack_script), f"{name()}:{SBX_HOME}/slack-notify.sh"])
        sbx(["exec", "-ti", name(), "sudo", "chmod", "755", f"{SBX_HOME}/slack-notify.sh"])

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

    for event in slack_events().split(","):
        event = event.replace(" ", "")
        if not event:
            continue
        if event not in SUPPORTED_SLACK_EVENTS:
            info()
            die(
                f"unsupported SBX_SLACK_EVENTS entry '{event}'\n"
                "       Supported events: " + ", ".join(SUPPORTED_SLACK_EVENTS)
            )


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
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
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
    """Reproduce the bash validation, returning (allowed, denied)."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProfileError(f"profile {where} must be a YAML mapping")

    for key in data:
        if key.lower() in FORBIDDEN_PROFILE_KEYS:
            raise ProfileError(f"profile {where} contains forbidden global key '{key}'")

    policies = data.get("policies") or {}
    if not isinstance(policies, dict):
        raise ProfileError(f"profile {where} 'policies' must be a mapping")
    for section in policies:
        if section.lower() != "network":
            raise ProfileError(
                f"profile {where} has unsupported policy section '{section}' (only 'network' is supported)"
            )

    network = policies.get("network") or {}
    if not isinstance(network, dict):
        raise ProfileError(f"profile {where} 'policies.network' must be a mapping")

    allowed, denied = [], []
    for action in ("allow", "deny"):
        entries = network.get(action) or []
        if not isinstance(entries, list):
            raise ProfileError(f"profile {where} 'policies.network.{action}' must be a list")
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ProfileError(f"profile {where} 'policies.network.{action}' contains invalid entry")
            (allowed if action == "allow" else denied).append(entry.strip())
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


def apply_profiles() -> None:
    if not profiles():
        return

    profiles_dir = Path(profile_directory()) if profile_directory() else script_dir() / "profiles"
    if not profiles_dir.is_dir():
        die(f"profile directory '{profiles_dir}' does not exist")

    banner(f"Applying profiles to {name()}")
    info()

    for raw_name in profiles().split(","):
        profile_name = raw_name.strip()
        if not profile_name:
            continue

        profile_path = ""
        for extension in ("yaml", "yml"):
            candidate = profiles_dir / f"{profile_name}.{extension}"
            if candidate.is_file():
                profile_path = str(candidate)
                break

        if not profile_path:
            die(f"profile '{profile_name}' not found in '{profiles_dir}'")

        info(f"Loading profile: {profile_name}")
        try:
            allowed, denied = load_profile(profile_path)
        except ProfileError as exc:
            die(str(exc))

        for domain in allowed:
            info(f"  allowing {domain}")
            sbx(["policy", "allow", "network", "--sandbox", name(), domain])
        for domain in denied:
            info(f"  denying {domain}")
            sbx(["policy", "deny", "network", "--sandbox", name(), domain])

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

    if DRY_RUN:
        banner("docker-sbx sandbox creation (dry-run)")
        info("dry-run: nothing will be changed")

    # -gsf writes a template and exits, before any other validation.
    if CONFIG.gensecretsfile:
        directory = os.path.dirname(CONFIG.gensecretsfile)
        if directory and not os.path.isdir(directory):
            die(f"directory '{directory}' does not exist")
        generate_secrets_file(CONFIG.gensecretsfile)
        return 0

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

    # Fail fast, before the secret scan and before anything is created. Individual
    # `sbx` calls are deliberately non-fatal (sbx() runs with check=False, as the
    # bash version did), so without this a missing sbx would sail through every step
    # doing nothing and still report success.
    if not DRY_RUN and not have("sbx"):
        die(
            "the sbx command was not found on PATH - run docker-sbx-install.py first, "
            "then open a new terminal so the PATH change is picked up"
        )

    volume_args = build_volume_args()
    check_project_secrets()
    create_sandbox(volume_args)
    setup_sandbox()
    update_sbx_policy()
    update_sbx_policy_slack()
    apply_profiles()
    return 0


if __name__ == "__main__":
    sys.exit(main())
