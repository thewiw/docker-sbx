#!/usr/bin/env python3
"""docker-sbx host setup - cross-platform.

One-time host setup for Docker Sandboxes (`sbx`): ensure Docker, install the
`sbx` CLI, sign in, set the global network policy, and persist the environment
variables the sandbox expects.

This is the cross-platform successor to docker-sbx-install.sh, which only ran on
Linux (it drove apt, `usermod -aG kvm` and the `sg kvm` wrapper). The function
names below deliberately mirror the bash ones so the two stay easy to diff while
both are in the tree.

Only the standard library is used, so the file can be copied to any machine with
a stock Python 3.9+.

Usage:
    sudo python3 docker-sbx-install.py          # Linux / WSL (needs root)
    python3 docker-sbx-install.py               # macOS (Homebrew refuses to run as root)
    python  docker-sbx-install.py               # Windows (from an elevated terminal)

    python3 docker-sbx-install.py --dry-run     # print the commands, change nothing
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LINUX, WSL, MACOS, WINDOWS = "linux", "wsl", "macos", "windows"

# Packages installed from Docker's own apt repository, and the conflicting
# packages the official instructions ask to remove first (distro builds of the
# same binaries, which would clash with them).
DOCKER_APT_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
DOCKER_CONFLICTING_PACKAGES = (
    "docker.io",
    "docker-compose",
    "docker-compose-v2",
    "docker-doc",
    "podman-docker",
    "containerd",
    "runc",
)

# Docker Desktop is only needed on macOS/Windows for the host-side gitleaks
# scan (`docker run` in docker-sbx-create-sandbox.sh); `sbx` itself is standalone.
DOCKER_DESKTOP_CASK = "docker"
DOCKER_DESKTOP_WINGET = "Docker.DockerDesktop"
SBX_WINGET = "Docker.sbx"

# Global network policy, applied once at install time. Sandboxes are allowed to
# do system updates; the deny list names the code hosts explicitly so it keeps
# holding even if the default policy is changed later.
ALLOW_DOMAINS = "archive.ubuntu.com,security.ubuntu.com,download.docker.com"
DENY_DOMAINS = (
    "github.com,github.org",
    "gitlab.com,gitlab.org",
    "atlassian.com,atlassian.net,bitbucket.com,bitbucket.org",
    "postman.com",
)

ENV_MARKER = "# docker-sbx setup env"

# Set by main() from the parsed arguments / the running interpreter.
DRY_RUN = False
PLATFORM = LINUX


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


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# Platform / identity / privilege
# --------------------------------------------------------------------------


def detect_platform() -> str:
    if os.name == "nt":
        return WINDOWS
    if platform.system() == "Darwin":
        return MACOS
    # WSL reports itself as Linux; the distinction only matters for messaging.
    if "microsoft" in platform.release().lower() or os.environ.get("WSL_DISTRO_NAME"):
        return WSL
    return LINUX


def is_linux_like() -> bool:
    return PLATFORM in (LINUX, WSL)


def invoking_user() -> str:
    """The human running the script, not root."""
    if is_linux_like():
        return os.environ.get("SUDO_USER") or getpass.getuser()
    return getpass.getuser()


def is_admin() -> bool:
    if PLATFORM == WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def require_admin() -> None:
    """Check for the privilege this platform's steps need, and explain how to get it.

    Deliberately check-and-instruct rather than self-elevating.
    """
    script = Path(sys.argv[0]).name

    if PLATFORM == WINDOWS:
        if is_admin():
            return
        banner("Error: this script must run from an elevated terminal")
        info("Open Windows Terminal / PowerShell with 'Run as administrator', then run again:")
        info(f"    python {script}")
        sys.exit(1)

    if PLATFORM == MACOS:
        # Homebrew refuses to run as root, so here root is the error, not the fix.
        if not is_admin():
            return
        banner("Error: do not run this script as root on macOS")
        info("Homebrew refuses to run as root. Re-run it as your normal user:")
        info(f"    python3 {script}")
        sys.exit(1)

    if is_admin():
        return
    banner("Error: This script must be run as root (use sudo)")
    info(f"    sudo python3 {script}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------


class _DryResult:
    """Stand-in for a CompletedProcess while --dry-run is active."""

    returncode = 0
    stdout = ""
    stderr = ""


def run(cmd, *, check: bool = True, capture: bool = False):
    """Run a command, echoing it first. Aborts on failure when check is set."""
    cmd = [str(c) for c in cmd]
    info("  $ " + " ".join(shlex.quote(c) for c in cmd))
    if DRY_RUN:
        return _DryResult()
    proc = subprocess.run(cmd, text=True, capture_output=capture)
    if check and proc.returncode != 0:
        die(f"command failed with exit code {proc.returncode}: " + " ".join(cmd))
    return proc


def have(command: str) -> bool:
    return shutil.which(command) is not None


def confirm(prompt: str) -> bool:
    """Ask a yes/no question.

    Mirrors the bash helper exactly: y/Y/yes/oui (case-insensitive) is yes;
    empty, EOF or anything else is no. Never exits - a "no" only skips an
    optional step.
    """
    if DRY_RUN:
        info(f"{prompt}(dry-run: assuming yes)")
        return True
    try:
        response = input(prompt)
    except (EOFError, KeyboardInterrupt):
        info("")
        return False
    return response.strip().lower() in ("o", "y", "yes", "oui")


def write_file(path: Path, content: str) -> None:
    """Write a file, creating its parent. Caller is already privileged."""
    info(f"  > writing {path}")
    if DRY_RUN:
        for line in content.splitlines():
            info(f"      {line}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --------------------------------------------------------------------------
# Docker
# --------------------------------------------------------------------------


def docker_installed() -> bool:
    return have("docker")


def docker_version() -> str:
    if DRY_RUN:
        return "(dry-run: not queried)"
    if not docker_installed():
        return "unknown"
    proc = run(["docker", "--version"], check=False, capture=True)
    out = proc.stdout or ""
    for token in out.split():
        parts = token.split(".")
        if len(parts) >= 2 and parts[0].isdigit():
            return token.strip(",")
    return "unknown"


def setup_docker_repo() -> None:
    """Point apt at Docker's official repository. Idempotent (Linux only)."""
    run(["apt-get", "update", "-y"])
    run(["apt-get", "install", "ca-certificates", "curl", "-y"])
    run(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
    run(
        [
            "curl",
            "-fsSL",
            "https://download.docker.com/linux/ubuntu/gpg",
            "-o",
            "/etc/apt/keyrings/docker.asc",
        ]
    )
    run(["chmod", "a+r", "/etc/apt/keyrings/docker.asc"])

    # The codename comes from /etc/os-release; fall back to the Ubuntu default.
    suites = os.environ.get("UBUNTU_CODENAME") or _os_release_value(
        "UBUNTU_CODENAME"
    ) or _os_release_value("VERSION_CODENAME") or "noble"
    # Docker's repository uses Debian arch names (amd64/arm64), which is what
    # `dpkg --print-architecture` returned in the bash version - platform.machine()
    # uses the kernel's names (x86_64/aarch64) instead.
    machine = (platform.machine() or "x86_64").lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "i686": "i386"}.get(
        machine, machine
    )

    content = "\n".join(
        [
            "Types: deb",
            "URIs: https://download.docker.com/linux/ubuntu",
            f"Suites: {suites}",
            "Components: stable",
            f"Architectures: {arch}",
            "Signed-By: /etc/apt/keyrings/docker.asc",
            "",
        ]
    )
    write_file(Path("/etc/apt/sources.list.d/docker.sources"), content)


def _os_release_value(key: str) -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def remove_conflicting_docker_packages() -> None:
    """Remove only the conflicting packages that are actually installed."""
    proc = run(
        ["dpkg", "--get-selections", *DOCKER_CONFLICTING_PACKAGES],
        check=False,
        capture=True,
    )
    present = [
        parts[0]
        for parts in (line.split() for line in (proc.stdout or "").splitlines())
        if len(parts) >= 2 and parts[1] == "install"
    ]
    if present:
        run(["apt-get", "remove", "-y", *present])


def install_docker() -> None:
    if not is_linux_like():
        install_docker_desktop_guarded()
        return

    banner("Docker installation in progress, please be patient...")
    remove_conflicting_docker_packages()
    setup_docker_repo()
    run(["apt-get", "update", "-y"])
    run(["apt-get", "install", *DOCKER_APT_PACKAGES, "-y"])
    run(["usermod", "-aG", "docker", invoking_user()])


def update_docker() -> None:
    if not is_linux_like():
        update_docker_desktop()
        return

    banner("Docker update in progress, please be patient...")
    # Ensure the official repository is configured, then upgrade in place.
    # --only-upgrade keeps this from installing anything not already present,
    # so an existing setup is never replaced by a different build.
    setup_docker_repo()
    run(["apt-get", "update", "-y"])
    run(["apt-get", "install", "--only-upgrade", *DOCKER_APT_PACKAGES, "-y"])
    run(["usermod", "-aG", "docker", invoking_user()])


def install_docker_desktop_guarded() -> None:
    banner("Docker Desktop")
    info(
        "The host needs Docker for the secret scan (docker run ... gitleaks) that "
        "docker-sbx-create-sandbox.sh performs."
    )
    if not confirm("🔍 Install Docker Desktop [y/N] "):
        info("Docker Desktop installation skipped.")
        warn(
            "the host secret scan will not work until Docker is installed - "
            "docker-sbx-create-sandbox.sh -s false skips it in the meantime"
        )
        return
    install_docker_desktop()


def install_docker_desktop() -> None:
    if PLATFORM == MACOS:
        if not have("brew"):
            die(
                "Homebrew is required to install Docker Desktop on macOS - see https://brew.sh"
            )
        run(["brew", "install", "--cask", DOCKER_DESKTOP_CASK])
    else:
        if not have("winget"):
            die("winget is required to install Docker Desktop - install 'App Installer' from the Microsoft Store")
        run(["winget", "install", "--accept-package-agreements", "--accept-source-agreements", DOCKER_DESKTOP_WINGET])


def update_docker_desktop() -> None:
    if PLATFORM == MACOS:
        if not have("brew"):
            die("Homebrew is required to update Docker Desktop on macOS - see https://brew.sh")
        run(["brew", "upgrade", "--cask", DOCKER_DESKTOP_CASK], check=False)
    else:
        if not have("winget"):
            die("winget is required to update Docker Desktop")
        run(["winget", "upgrade", "--accept-package-agreements", "--accept-source-agreements", DOCKER_DESKTOP_WINGET], check=False)


def install_or_update_docker() -> None:
    """Fresh install when Docker is absent, in-place update when it is present.

    A refusal to update is not an error: the rest of the installation carries on.
    """
    if not docker_installed():
        install_docker()
        return

    banner("Docker is already installed on this system")
    info(f"Binary  : {shutil.which('docker')}")
    info(f"Version : {docker_version()}")
    info()
    info("It will be updated in place: existing images and containers are kept.")

    if confirm("🔍 Update the existing Docker installation [y/N] "):
        info()
        update_docker()
    else:
        info("Docker update skipped, continuing with the rest of the installation.")
        info()


# --------------------------------------------------------------------------
# sbx
# --------------------------------------------------------------------------


def run_sbx(args, *, check: bool = True, capture: bool = False):
    """Run `sbx ...` as the invoking user.

    On Linux the user was just added to the kvm group, which the current session
    does not have yet, so - exactly like the bash script - the command goes
    through `su - <user> -c "sg kvm -c '...'"`. Elsewhere it is a plain call.
    """
    argv = ["sbx", *args]
    if is_linux_like() and is_admin():
        inner = "sg kvm -c " + shlex.quote(" ".join(shlex.quote(a) for a in argv))
        argv = ["su", "-", invoking_user(), "-c", inner]
    return run(argv, check=check, capture=capture)


def install_sbx() -> None:
    banner("Docker sandboxes installation in progress, please be patient...")

    if is_linux_like():
        # REPO_ONLY=1 runs just the repository setup from Docker's convenience script.
        run(["sh", "-c", "curl -fsSL https://get.docker.com | REPO_ONLY=1 sh"])
        run(["apt-get", "install", "util-linux-extra", "docker-sbx", "python3-yaml", "-y"])
        run(["usermod", "-aG", "kvm", invoking_user()])
    elif PLATFORM == MACOS:
        if not have("brew"):
            die("Homebrew is required to install sbx on macOS - see https://brew.sh")
        run(["brew", "trust", "docker/tap"])
        run(["brew", "install", "docker/tap/sbx"])
    else:
        if not have("winget"):
            die("winget is required to install sbx - install 'App Installer' from the Microsoft Store")
        run(["winget", "install", "-h", "--accept-package-agreements", "--accept-source-agreements", SBX_WINGET])
        enable_hypervisor_platform()


def enable_hypervisor_platform() -> None:
    """Windows only: local sandboxes need the Hypervisor Platform feature."""
    banner("Enabling Windows Hypervisor Platform (required for local sandboxes)")
    run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All",
        ],
        check=False,
    )
    warn("a reboot may be required before local sandboxes can start")


def sbx_login() -> None:
    # Interactive OAuth: it opens a browser, so it needs a real terminal. Failure
    # is not fatal here - no sandbox can be created without a login, but the
    # remaining setup steps are still worth completing.
    run_sbx(["login"], check=False)


def set_default_policy() -> None:
    """Enforce deny-all as the global default.

    `sbx policy init <preset>` is the documented form on current CLIs; older ones
    only have `set-default`. Try the current one and fall back, reporting which
    was used rather than failing the whole install.
    """
    proc = run_sbx(["policy", "init", "deny-all"], check=False, capture=True)
    if proc.returncode == 0:
        return
    warn("'sbx policy init deny-all' failed, falling back to 'sbx policy set-default deny-all'")
    run_sbx(["policy", "set-default", "deny-all"], check=False)


def setup_sbx_policy() -> None:
    banner("Setting the global sandbox network policy")

    set_default_policy()

    info(f"  allowing {ALLOW_DOMAINS}")
    run_sbx(["policy", "allow", "network", ALLOW_DOMAINS], check=False)

    for domains in DENY_DOMAINS:
        info(f"  denying {domains}")
        run_sbx(["policy", "deny", "network", domains], check=False)


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def user_home() -> str:
    """Home directory of the human running the script, not of root."""
    user = invoking_user()
    try:
        import pwd

        return pwd.getpwnam(user).pw_dir
    except (ImportError, KeyError):
        return str(Path.home())


def profile_path() -> Path:
    """The shell startup file to persist the environment variables in.

    Resolved against the invoking user's home, not root's: the script runs under
    sudo, where `Path.home()` is /root, so the variables would land in the wrong
    profile (which is what the bash version does).
    """
    home = Path(user_home())
    if PLATFORM == MACOS:
        # macOS has defaulted to zsh since Catalina; older setups still use bash.
        if os.environ.get("SHELL", "").endswith("zsh"):
            return home / ".zshrc"
        return home / ".bash_profile"
    return home / ".bashrc"


def env_values():
    # C.utf8 is a Linux locale name; macOS spells it C.UTF-8. On Windows these
    # only matter when the host is reached from WSL, but set them for parity.
    locale = "C.UTF-8" if PLATFORM == MACOS else "C.utf8"
    return (("SBX_NO_TELEMETRY", "1"), ("LANG", locale), ("LC_ALL", locale))


def setup_env() -> None:
    banner("Persisting environment variables")

    if PLATFORM == WINDOWS:
        for name, value in env_values():
            run(["setx", name, value], check=False)
        return

    # Idempotent: guarded by a marker comment rather than by grepping for the
    # values, so a LANG or LC_ALL the user set themselves is never re-appended.
    path = profile_path()
    existing = ""
    if path.exists():
        existing = path.read_text(errors="replace")
    if ENV_MARKER in existing:
        info(f"{ENV_MARKER} already present in {path}, leaving it unchanged")
        return

    block = [ENV_MARKER]
    block += [f'export {name}="{value}"' for name, value in env_values()]
    content = existing
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n".join(block) + "\n"

    if DRY_RUN:
        info(f"  > appending to {path}:")
        for line in block:
            info(f"      {line}")
        return
    path.write_text(content)
    info(f"Appended to {path}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    global DRY_RUN, PLATFORM

    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="One-time host setup for Docker Sandboxes (sbx).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every command that would run, and change nothing",
    )
    args = parser.parse_args(argv)

    DRY_RUN = args.dry_run
    PLATFORM = detect_platform()

    banner(f"docker-sbx host setup ({PLATFORM})")
    if DRY_RUN:
        info("dry-run: nothing will be changed")

    require_admin()
    install_or_update_docker()
    install_sbx()
    sbx_login()
    setup_sbx_policy()
    setup_env()

    banner(
        "Installation should be done, it is recommended to logout/login "
        "for changes to take effect"
    )
    info()
    return 0


if __name__ == "__main__":
    sys.exit(main())
