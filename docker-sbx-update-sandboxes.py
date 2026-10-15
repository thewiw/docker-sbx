#!/usr/bin/env python3
"""Refresh packages inside sbx (Docker Sandbox) environments - cross-platform.

For each sandbox it runs, in one `sbx exec` call:

    sudo apt update  ->  sudo apt upgrade -y  ->  sudo apt autoremove -y

This is the cross-platform successor to docker-sbx-update-sandboxes.sh, which was
Linux-only (it leaned on an unquoted bash array for the exec flags and on
`mapfile`/`read -r -a` for the sandbox lists). The function names below
deliberately mirror the bash ones so the two stay easy to diff while both are in
the tree.

Usage:
    python3 docker-sbx-update-sandboxes.py                          # every sandbox
    python3 docker-sbx-update-sandboxes.py -i bc,web-1             # only these
    python3 docker-sbx-update-sandboxes.py -x bc,web-1             # all except these
    python3 docker-sbx-update-sandboxes.py -i bc,web-1 -x bc       # bc filtered out
    python3 docker-sbx-update-sandboxes.py --help

Recommended invocation (sbx wants a real TTY):

    script -q /dev/null python3 docker-sbx-update-sandboxes.py [OPTIONS]

!! Never write the chain unquoted on your shell command line:
     sbx exec -ti box apt update && apt upgrade -y && apt autoremove
   runs only the first command in the sandbox; the other two run ON THIS HOST.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def info(msg: str = "") -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# 0. Usage
# --------------------------------------------------------------------------


def usage_text() -> str:
    program = Path(sys.argv[0]).name
    return "\n".join(
        [
            f"Usage: {program} [--help] [--dry-run] [-i LIST] [-x LIST]",
            "",
            "With no -i, every sandbox returned by 'sbx ls -q' is refreshed.",
            "With -i, only the listed sandboxes are processed and 'sbx ls' is never",
            "called (useful when listing is slow or unreliable).",
            "",
            "  -i, --include LIST   comma- or space-separated sandbox names to UPDATE.",
            "                       Repeatable. Merged with the INCLUDE env var.",
            '                       Omit it to mean "all sandboxes".',
            "  -x, --exclude LIST   comma- or space-separated sandbox names to SKIP.",
            "                       Repeatable. Merged with the SKIP_LIST env var.",
            "                       Exclusions win over inclusions.",
            "",
            "  --dry-run            print what would run, change nothing (same as DRY_RUN=1)",
            "",
            "Environment options:",
            "  CLOUD=1                target cloud sandboxes (adds global --cloud)",
            "  DRY_RUN=1              print what would run, change nothing",
            '  INCLUDE="a b"          same as -i, for env-based configuration',
            '  SKIP_LIST="a b"        same as -x, for env-based configuration',
            "  SBX_TIMEOUT=120        seconds allowed per query call",
            "  SBX_EXEC_FLAGS=-ti     force exec flags instead of auto-detection",
            "  SBX_LS_CMD='...'       custom listing command (names on stdout)",
            "  APT_CHAIN='...'        custom in-sandbox command chain",
            "  APT=apt-get|apt        package manager",
            "  SUDO='sudo'            or 'sudo -n' to never prompt (fails fast instead)",
        ]
    )


def usage(stream=None) -> None:
    print(usage_text(), file=stream if stream is not None else sys.stdout)


# --------------------------------------------------------------------------
# 1. Command line
# --------------------------------------------------------------------------


class UsageError(Exception):
    """A bad command line, reported the way the bash script reports it (exit 2)."""


class Config:
    """Everything the script reads from the environment, plus the parsed flags."""

    def __init__(self, env=None) -> None:
        env = os.environ if env is None else env
        # Seeded from the environment, then extended by -i / -x.
        self.include_raw = env.get("INCLUDE", "")
        self.exclude_raw = env.get("SKIP_LIST", "")
        self.dry_run = env.get("DRY_RUN", "0")
        self.cloud = env.get("CLOUD", "0")
        self.apt = env.get("APT", "apt-get")  # script-friendly; 'apt' also works
        self.sudo = env.get("SUDO", "sudo")  # sandbox agents are unprivileged; apt needs root
        self.sbx_timeout = env.get("SBX_TIMEOUT", "120")  # queries only
        self.sbx_ls_cmd = env.get("SBX_LS_CMD", "")
        self.sbx_exec_flags = env.get("SBX_EXEC_FLAGS", "")
        self.apt_chain = env.get("APT_CHAIN", "")
        self.show_help = False
        # Normalised once, after parsing.
        self.include = []
        self.exclude = []


def normalize_list(raw: str):
    """Split a comma/whitespace separated list into tokens, in order.

    The bash script folds commas to spaces, squeezes runs of whitespace, then pads
    both ends with a space so `case "$LIST" in *" $name "*)` works. That padding is
    just an awkward set membership; splitting gives the same tokens, and keeping
    the order (and any duplicates) preserves the "Including N sandbox(es): a b c"
    output exactly.
    """
    return raw.replace(",", " ").split()


def parse_args(argv, env=None) -> Config:
    """Parse the command line, mirroring the bash while-loop and its messages."""
    cfg = Config(env)
    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1

        if arg in ("-h", "--help"):
            cfg.show_help = True
            return cfg
        if arg == "--dry-run":
            cfg.dry_run = "1"
            continue
        if arg == "--":
            continue
        if arg.startswith("--include="):
            cfg.include_raw += " " + arg[len("--include=") :]
            continue
        if arg in ("--include", "-i"):
            if index >= len(argv):
                raise UsageError(f"missing sandbox list after {arg}")
            cfg.include_raw += " " + argv[index]
            index += 1
            continue
        if arg.startswith("--exclude="):
            cfg.exclude_raw += " " + arg[len("--exclude=") :]
            continue
        if arg in ("--exclude", "-x"):
            if index >= len(argv):
                raise UsageError(f"missing sandbox list after {arg}")
            cfg.exclude_raw += " " + argv[index]
            index += 1
            continue
        if arg.startswith("-"):
            raise UsageError(f"unknown option: {arg}")
        raise UsageError(f"unexpected argument: {arg} (use -i/--include to name sandboxes)")

    cfg.include = normalize_list(cfg.include_raw)
    cfg.exclude = normalize_list(cfg.exclude_raw)
    return cfg


# --------------------------------------------------------------------------
# 2. Configuration
# --------------------------------------------------------------------------

# Executed inside each sandbox as a single shell command string. Each apt command
# gets its own $SUDO prefix. The env assignment must come AFTER sudo (sudo parses
# it for the command); 'DEBIAN_FRONTEND=... sudo apt' would have the variable
# stripped by sudo's env_reset.
#
# The run-aligned padding is kept verbatim from the bash assignment: the shell does
# not care about it, but --dry-run prints this string, and matching it keeps the
# bash and Python dry runs diffable line for line.
APT_CHAIN_TEMPLATE = (
    "echo '==> sudo apt update'        && $SUDO DEBIAN_FRONTEND=noninteractive $APT update         && "
    "echo '==> sudo apt upgrade'       && $SUDO DEBIAN_FRONTEND=noninteractive $APT -y upgrade     && "
    "echo '==> sudo apt autoremove'    && $SUDO DEBIAN_FRONTEND=noninteractive $APT -y autoremove"
)


def build_apt_chain(cfg: Config) -> str:
    """The in-sandbox command string, with $SUDO and $APT already substituted.

    bash expands those two in the `${APT_CHAIN:-...}` default, so the value handed
    to `sbx exec` always has the concrete prefix and package manager in it.
    """
    if cfg.apt_chain:
        return cfg.apt_chain
    return APT_CHAIN_TEMPLATE.replace("$SUDO", cfg.sudo).replace("$APT", cfg.apt)


def build_sbx(cfg: Config):
    """The sbx command prefix, with the global --cloud option when asked for."""
    return ["sbx", "--cloud"] if cfg.cloud == "1" else ["sbx"]


def timeout_seconds(raw: str) -> float:
    """SBX_TIMEOUT as a number of seconds, for Python's own subprocess timeout.

    The bash script hands the value to `timeout(1)`; here Python measures it, so the
    value is validated once, up front, instead of reaching a command line per query.
    """
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise UsageError(f"SBX_TIMEOUT must be a number of seconds, got '{raw}'")
    if seconds <= 0:
        raise UsageError(f"SBX_TIMEOUT must be greater than 0, got '{raw}'")
    return seconds


def detect_exec_flags(explicit: str, stdin_tty: bool, stdout_tty: bool) -> str:
    """-t needs a terminal; -i alone is the safe non-interactive default."""
    if explicit:
        return explicit
    return "-ti" if (stdin_tty and stdout_tty) else "-i"


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------


def run(cmd, *, capture: bool = False, stdin=None, timeout=None):
    """Run a command, capturing stdout only when asked (stderr stays visible).

    `timeout` is measured by Python, not by a `timeout(1)` binary. Windows ships an
    unrelated `timeout.exe` under that name - it *waits* N seconds - so looking one
    up by name found it and prefixed every query with `timeout 120 sbx ...`, which it
    rejected. Every query then came back failed and the script reported "no
    sandboxes found". A timeout is reported as exit code 124, the status GNU
    timeout(1) uses, so callers still handle one shape.
    """
    # Flush first: our stdout is block-buffered when piped, while the child writes
    # straight to the inherited fd. Without this, "Preflight ..." lands *after* the
    # output of the sbx call it precedes.
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [str(c) for c in cmd]
    kwargs = {"text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
    if stdin is not None:
        kwargs["stdin"] = stdin
    try:
        return subprocess.run(argv, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")


def sbx_run(cfg: Config, sbx, args, *, capture: bool = False):
    """Queries: time-limited, non-interactive, stdin closed."""
    return run(
        [*sbx, *args],
        capture=capture,
        stdin=subprocess.DEVNULL,
        timeout=timeout_seconds(cfg.sbx_timeout),
    )


def sbx_exec(sbx, exec_flags: str, name: str, cmd: str) -> int:
    """The apt chain: no timeout, inherited stdio.

    A TTY-attached exec in a background process group gets stopped by SIGTTIN,
    which looks exactly like a hang - hence no timeout here. stdin stays inherited
    so a sudo password prompt is answerable when SUDO='sudo'.

    `sh -c` is the shell *inside* the sandbox, so it stays `sh -c` on every host.
    """
    argv = [*sbx, "exec", *exec_flags.split(), name, "sh", "-c", cmd]
    return run(argv).returncode


# --------------------------------------------------------------------------
# 3. Enumerate sandboxes
# --------------------------------------------------------------------------


def platform_shell():
    """The host shell, for a user-supplied SBX_LS_CMD (sh on POSIX, cmd on Windows)."""
    return ["cmd", "/c"] if os.name == "nt" else ["sh", "-c"]


def clean_names(text: str):
    """Trim each line, drop the blanks, de-duplicate, sort."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            names.append(line)
    return sorted(set(names))


def extract_json_names(text: str):
    """Every `name`/`Name` string in a JSON document, at any depth.

    jq's `.. | objects | (.name // .Name // empty)` - the first of the two keys
    that is neither null nor false wins. This replaces both bash paths (the jq
    one and the `grep -oE` fallback), so jq is no longer a dependency.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []

    names = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key in ("name", "Name"):
                if key in node and node[key] is not None and node[key] is not False:
                    value = node[key]
                    if isinstance(value, str) and value:
                        names.append(value)
                    break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return names


def parse_table_names(text: str):
    """First column of the human-readable `sbx ls` table, minus the header."""
    headers = {"NAME", "ID", "SANDBOX", "AGENT"}
    names = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        first = fields[0]
        if first in headers:
            continue
        if re.fullmatch(r"[-─=]+", first):
            continue
        names.append(first)
    return sorted(set(names))


def list_sandboxes(cfg: Config, sbx):
    """Sandbox names, from the first of four sources that answers.

    Each source is stripped and de-duplicated. The final fallback always yields a
    list (even an empty one), so - exactly as in bash - a broken `sbx` surfaces as
    "no sandboxes found" rather than as a listing error.
    """
    if cfg.sbx_ls_cmd:
        proc = run([*platform_shell(), cfg.sbx_ls_cmd], capture=True)
        if proc.returncode == 0:
            return clean_names(proc.stdout)

    # Documented "names only" output.
    proc = sbx_run(cfg, sbx, ["ls", "-q"], capture=True)
    if proc.returncode == 0:
        return clean_names(proc.stdout)

    # Fallback: JSON output.
    proc = sbx_run(cfg, sbx, ["ls", "--json"], capture=True)
    if proc.returncode == 0 and ("{" in proc.stdout or "[" in proc.stdout):
        return sorted(set(extract_json_names(proc.stdout)))

    # Last resort: first column of the human-readable table.
    proc = sbx_run(cfg, sbx, ["ls"], capture=True)
    return parse_table_names(proc.stdout)


# --------------------------------------------------------------------------
# 4. Main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        cfg = parse_args(argv)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        usage(sys.stderr)
        return 2

    if cfg.show_help:
        usage(sys.stdout)
        return 0

    # Reject a bad SBX_TIMEOUT before the first query: it is an environment problem,
    # not a command line one, so no usage dump - but it must not be discovered by
    # watching every query fail one by one.
    try:
        timeout_seconds(cfg.sbx_timeout)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2

    sbx = build_sbx(cfg)
    exec_flags = detect_exec_flags(cfg.sbx_exec_flags, sys.stdin.isatty(), sys.stdout.isatty())
    apt_chain = build_apt_chain(cfg)

    info(f"Preflight (exec flags: {exec_flags}, sudo prefix: '{cfg.sudo}')")
    sbx_run(cfg, sbx, ["version"])
    sbx_run(cfg, sbx, ["daemon", "status"])

    if cfg.include:
        # Explicit -i list: no sbx ls, no preflight listing.
        sandboxes = list(cfg.include)
        info(f"Including {len(sandboxes)} sandbox(es): " + " ".join(sandboxes))
    else:
        # No -i: list everything.
        sandboxes = list_sandboxes(cfg, sbx)
        if not sandboxes:
            print("no sandboxes found", file=sys.stderr)
            return 0
        info(f"Found {len(sandboxes)} sandbox(es): " + " ".join(sandboxes))

    if cfg.exclude:
        info("Excluding: " + " ".join(cfg.exclude))

    # Warn about names present in both lists (exclusion wins).
    for included in cfg.include:
        if included in cfg.exclude:
            info(f"note: {included} is both included and excluded - it will be skipped")

    ok = failed = skipped = 0

    for sandbox in sandboxes:
        if sandbox in cfg.exclude:
            info(f"skip {sandbox} (excluded)")
            skipped += 1
            continue

        info(f"== {sandbox} ==")

        if cfg.dry_run == "1":
            info(f"   would run: {' '.join(sbx)} exec {exec_flags} {sandbox} sh -c")
            info(f"   {apt_chain}")
            continue

        returncode = sbx_exec(sbx, exec_flags, sandbox, apt_chain)
        if returncode == 0:
            ok += 1
        else:
            print(f"   FAILED (rc={returncode})", file=sys.stderr)
            failed += 1

    info(f"Done. ok={ok} failed={failed} skipped={skipped}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
