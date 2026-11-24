# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains scripts that install the [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine and automate creation of AI agent sandboxes using Claude Code. Each sandbox is an isolated Docker container with a shared project volume and a restrictive network policy.

The bash scripts are tested on WSL Ubuntu 24.04/26.04 LTS, native Ubuntu 26.04 LTS and Windows 11. The host scripts are being migrated to cross-platform Python (Linux/WSL, macOS, Windows).

## Scripts

Scripts that run on the **host** live in the repository root and are run from there. Scripts that run **inside** the sandbox live in `sandbox/` and are staged into the sandbox by `docker-sbx-create-sandbox.sh`; they are never run on the host. Host scripts are being migrated from bash to cross-platform Python (`*.py`, Python 3.9+, standard library only); the `.sh` files are kept until the ports are validated. Test suites live in `test/`.

| Script / Directory | Purpose |
|--------|---------|
| `docker-sbx-install.sh` | One-time host setup. Installs Docker CE and the `docker-sbx` engine, creates a `kvm` group user, and sets default network policies (deny-all, allow only Ubuntu archives and Docker download). Must be run with `sudo`. |
| `docker-sbx-install.py` | Cross-platform replacement for `docker-sbx-install.sh`, runnable on Linux/WSL, macOS and Windows (Python 3.9+, standard library only). Does the same setup: installs or updates Docker, installs the `sbx` CLI, signs in, sets the global network policy, and persists the environment variables. `--dry-run` prints every command and changes nothing, and needs no privilege on any platform. Needs `sudo` on Linux; runs as a regular user on Windows and macOS (Homebrew refuses to run as root). The only Windows step needing administrator rights — enabling the Windows Hypervisor Platform — is probed first and demanded only when the feature is missing. |
| `docker-sbx-create-sandbox.sh` | Creates a new sandbox for a project. Scans the project for secrets (file patterns + gitleaks), runs `sbx create`, then copies `sandbox/docker-sbx-setup-sandbox.sh` into the sandbox and executes it. Optionally applies per-sandbox policy profiles. |
| `docker-sbx-create-sandbox.py` | Cross-platform replacement for `docker-sbx-create-sandbox.sh` (Linux/WSL, macOS, Windows; Python 3.9+, standard library only). Same options and the same user-facing messages; `--dry-run` prints every command and creates nothing. Profiles are YAML: PyYAML is used when importable, otherwise a small parser covers the documented profile subset. A profile is either a flat `<name>.yaml` (network only) or a directory `<name>/` with a `profile.yaml` plus an asset tree (`agent/`, `project/`, and any other root entry at agent level), installed through a host-built tarball. A directory profile may also declare `kit: <dir>`, whose directory is passed to `sbx create --kit` at creation and excluded from the asset tree; `flutter-sdk` is the shipped example. Flat policies are selected with `-profile-policy`, directory bundles with `-profile-directory`; each flag refuses the other kind, and `-profiles-dir` sets the base directory. The saved config records `profiles:` (policies), `profile_directories:` (bundles) and `profiles_dir:`. `--help` scans the active profiles directory and lists both kinds with each profile's `description`. After creating a sandbox it records a per-sandbox config (unless `--no-config`); `--recreate` rebuilds one from its saved config, and `--from-config` creates from a config. It waits for the new sandbox's first start to finish before configuring it, refuses to reuse an existing sandbox name, and treats a failed `sbx create` as fatal — the bash version's `sbx()` calls are non-fatal (mirroring the original), so a failed create used to be followed by setup against the *previous* sandbox of that name. Its `main_update()` is the implementation behind `docker-sbx-apply-profile.py`. |
| `docker-sbx-apply-profile.py` | Applies policy profiles (network rules and Claude assets) to an existing sandbox: `-n`, `-profile`, `-profile-directory`, `--config-dir`, `--no-config`, `--dry-run`. It never creates a sandbox, never scans for secrets and never re-runs the in-sandbox setup. It reads the saved config for the project path (project-level assets) and for a recorded profile directory, validates every named profile before changing anything, starts/waits for a stopped sandbox, then records the names and the effective directory in the config and prints `Config updated: <path>`. A kit-bearing profile is refused: kits are create-time only. The command is a thin entry point: it loads `docker-sbx-create-sandbox.py` from its own directory (a hyphenated filename cannot be imported by name) and delegates to `main_update()`, so creation and updates share one profile loader, asset packer and config writer. |
| `sandbox/docker-sbx-setup-sandbox.sh` | Runs **inside** the sandbox, after the host has waited for the sandbox's first start to finish. Takes the project path, an optional env-file path (or `-`), and the protected-paths flag. Installs `jq` **only if it is missing** — that is the only `apt` use, and it never kills `apt`/`dpkg` or upgrades packages, because the sandbox runs its own `apt` at boot (`/etc/durable-startup.d/001-startup-claude/001-cmd.sh`) and is still installing its agent during that first start. Writes Claude Code telemetry/opt-out settings into `~/.claude/settings.json`. It deliberately does **not** create a `~/workspace` symlink: the project is already mounted at its host path. Also installs the optional Slack and/or Nextcloud Talk notification hooks when the env file asks for them. The two providers share one body of code here (`write_notify_env`, `wire_notify_hooks`, `hook_var`) parameterised by variable prefix; each has its own installer gated by its own `<PREFIX>_ENABLED`, so a sandbox can run Slack only, Talk only, or both, and each wires its own hook script without disturbing the other. The protected-path guard is installed by default (`install_protected_paths_hook`); `false` removes an existing entry. |
| `sandbox/docker-sbx-guard-protected-paths.sh` | Runs **inside** the sandbox as `~/.claude/hooks/guard-protected-paths.sh`. Claude Code invokes it as a `PreToolUse` hook for `Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` and `Grep`; it blocks a call whose command, file path or targeted glob names `GoogleService-Info.plist`, `google-services.json` or `launch.json` (exit 2 with the reason on stderr). This is a text match, not a security boundary: variables, helper scripts and globs can bypass it. |
| `sandbox/docker-sbx-slack-notify.sh` | Runs **inside** the sandbox as `~/.claude/hooks/slack-notify.sh`. Invoked by Claude Code as a hook with the event payload on stdin; posts one short line per lifecycle event to Slack. Only installed when the optional feature is enabled via the env file. |
| `sandbox/docker-sbx-nctalk-notify.sh` | Runs **inside** the sandbox as `~/.claude/hooks/nctalk-notify.sh`. The Nextcloud Talk counterpart of the Slack hook — same architecture, same event vocabulary and message format — posting via the Talk spreed OCS API (`<url>/ocs/v2.php/apps/spreed/api/v1/chat/<room>`) with the app password as basic auth. All its variables carry the `SBX_NCTALK_` prefix and `SBX_NCTALK_ENABLED` is its master switch. Only installed when the feature is enabled via the env file; independent of the Slack hook. |
| `docker-sbx-update-sandboxes.sh` | Maintenance helper. Runs `apt update/upgrade/autoremove` inside existing sandboxes via `sbx exec`, one at a time. Enumerates with `sbx ls` (or a custom `SBX_LS_CMD`), selectable with `-i`/`-x`; `DRY_RUN=1` prints instead of running. Not part of sandbox creation. |
| `docker-sbx-update-sandboxes.py` | Cross-platform replacement for `docker-sbx-update-sandboxes.sh` (Linux/WSL, macOS, Windows; Python 3.9+, standard library only). Same options, the same user-facing messages and the same exit codes (0 clean, 1 when a sandbox fails, 2 on bad usage); adds a `--dry-run` flag alongside `DRY_RUN=1`. Lists sandboxes from `SBX_LS_CMD`, then `sbx ls -q`, then `sbx ls --json`, then the human-readable table — the JSON branch walks the parsed document itself, so `jq` is no longer needed. `SBX_LS_CMD` runs through the platform shell (`sh -c`, or `cmd /c` on Windows); the `sh -c` handed to `sbx exec` stays `sh -c`, since that one runs inside the sandbox. The query timeout (`SBX_TIMEOUT`) is enforced by `subprocess`, **not** by a `timeout(1)` binary: Windows ships an unrelated `timeout.exe` (it *waits* N seconds) under that name, so looking one up by name found it and prefixed every query with `timeout 120 sbx …`, which Windows rejected — every query failed and the script reported "no sandboxes found". A `SBX_TIMEOUT` that is not a positive number now exits 2 before the first query instead of failing each one. |
| `test/run-tests.sh` | Test suite for the Slack and Nextcloud Talk notification features, the env-file contract and the `create-sandbox` CLI. Exercises the hooks, the settings merge (including both providers enabled together), failure logging, and a real POST to a loopback sink. It also covers the profile-bundle, per-sandbox config and update/recreate/from-config paths — asset tarball assembly, the config round-trip, the apply-profile command, the create CLI's modes, and the protected-path guard's payload handling and settings wiring. The CLI-level tests now run against the Python implementation only — the bash host script was deleted when the migration completed — but `SBX_IMPLS` still selects, so a checkout that has both runs the same assertions against each and catches them drifting apart. Run directly on the host (`bash test/run-tests.sh`); no sandbox required. |
| `test/test_docker_sbx_install.py` | Unit tests for the pure helpers in `docker-sbx-install.py` (prompt parsing, platform/locale selection, profile path, environment-block idempotency). Run with `python3 -m unittest discover -s test -p 'test_*.py'`. |
| `test/test_create_sandbox.py` | Unit tests for the pure helpers in `docker-sbx-create-sandbox.py` (argument parsing and mode selection, env-file reading, volume specs, Slack and Nextcloud Talk validation, host extraction, the provider-script staging, the secrets-file scanner, the minimal YAML profile parser, profile-directory resolution, the `kit:` schema and the asset plan with its refused paths and size cap, the sandbox-config schema and round-trip, the `profiles:` / `profile_directories:` line-surgery merge, the `protected_paths` flag, the sandbox-name pre-check and the wait for a sandbox's first start). It also covers the update flag validator; the update flow itself lives in `test_apply_profile.py`. Same command as above. |
| `test/test_apply_profile.py` | Unit tests for `docker-sbx-apply-profile.py` and `main_update()`: argument and help handling, rejection of create-only flags and unknown options, rejection of kit-bearing profiles, delegation from the entry point (argv and exit code), the missing-implementation error, and the update flow itself — no `sbx create`, config-derived project/profiles directory, missing sandbox, failed `sbx` query reported separately from a missing sandbox, and dry-run. Same command as above; no `sbx` and no sandbox. |
| `test/test_update_sandboxes.py` | Unit tests for the pure helpers in `docker-sbx-update-sandboxes.py` (list normalisation, CLI parsing and its exit-2 messages, the apt chain, exec-flag detection, and the three sandbox-name extraction paths). Same command as above; needs no `sbx` and no sandbox. |
| `profiles/` | Policy profiles used by `docker-sbx-create-sandbox.py`. Most shipped language profiles are flat `<name>.yaml` files granting access to language-specific external resources; `profiles/example/` is a directory bundle carrying agent- and project-level assets and serves as the copyable template; `profiles/flutter-sdk/` is a directory bundle with a `kit:` that installs the Flutter/Dart/Android SDK toolchain at creation (its `flutter/README.md` documents versions, licences and limits). |

## Security Model

- **Default network policy**: deny-all. Only `archive.ubuntu.com`, `security.ubuntu.com`, and `download.docker.com` are allowed globally. Explicit deny rules are also set for GitHub, GitLab, Bitbucket, and Postman domains.
- **Secret scanning**: before a sandbox is created, the script checks for credential files (`google-services.json`, `key.properties`, `*.jks`, `*.cert`, `*.crt`, `*.key`) and runs `gitleaks` against the project's git repository. Creation aborts if secrets are found.
- **Protected paths** (default on, `-protected-paths false` to skip): the sandbox installs a `PreToolUse` hook that blocks `Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` and `Grep` calls naming `GoogleService-Info.plist`, `google-services.json` or `launch.json`. It is a guard against accidental access, not a boundary; a variable, helper script or glob can bypass a text match. The setting is recorded in the saved config and the hook is wired idempotently.
- **Shared volume**: the project directory is mounted as a shared volume. Claude Code has write access to the entire sandbox filesystem, including this volume. Do not place credentials inside the project path or in git history.
- **Profile assets**: a selected profile bundle can install arbitrary regular files under `/home/agent/.claude/` and `<project>/.claude/`. Project-level assets are written into the shared volume and are visible on the host, so a bundle is chosen with the same care as the project itself. Symlinks and special files are refused; agent-level `settings.json`, `settings.local.json` and `.credentials.json` are refused (docker-sbx and Claude Code own them), as are project-level `.git` and `.credentials.json` (`.git` is refused at both levels, so a bundle
pointed at a checkout cannot ship its history); and a bundle is capped at 2000 files / 32 MiB. Only profiles the user names with `-profile-policy` or `-profile-directory` are installed.
- **Profile kits**: a directory profile may name a `kit:` directory. Its Dockerfile is built by `sbx create --kit` on the host with Buildx, outside the sandbox's network policy, and the resulting image is added to the sandbox. The kit source is trusted the same way a profile is: a kit can run build steps on the host and add arbitrary files to the sandbox image. The shipped `flutter-sdk` kit is pinned and self-contained; `--kit` is an experimental `sbx` feature (v3 kits need sbx >= 0.45, 0.47+ recommended). The kit directory is never copied into `~/.claude`, and a kit-bearing profile is refused by `docker-sbx-apply-profile.py` because kits are create-time only.
- **Per-sandbox config**: `<config-dir>/<name>.yaml` records how to rebuild a sandbox. It stores **paths only, never credentials**: `env_file` and `secrets_settings` name the files that hold them. It is written mode `600` where the platform supports it and is editable by hand; `version` is `1`, unknown keys are rejected, and path-valued fields must be absolute.
- **Slack notifications** (optional): when enabled, the sandbox's own network policy is extended with just the endpoint the configured credential style needs (`hooks.slack.com` for a webhook, `slack.com` for a bot token) — no global rule is added. The credential is written to `~/.claude/hooks/slack.env`, mode `600`, outside the project volume. It is still readable by the agent in that sandbox, so scope the credential to what you are willing to expose there: an incoming webhook bound to a single channel is least privilege, a bot token (`chat:write`) can post anywhere the bot is a member. `docker-sbx-create-sandbox.py` aborts if `SBX_SLACK_ENABLED=true` but no usable target is configured, rather than silently installing a hook that can never deliver.
- **Nextcloud Talk notifications** (optional): the second provider, independent of Slack. When `SBX_NCTALK_ENABLED=true`, the sandbox's network policy is extended with the Nextcloud instance's host only (from `SBX_NCTALK_URL`) — no global rule. The Nextcloud user, app password and room token are written to `~/.claude/hooks/nctalk.env`, mode `600`, outside the project volume and separate from `slack.env`; the app password is still readable by the agent in that sandbox, so scope it to an account you are willing to expose there — it can act as that user for every API call. Creation aborts, naming the missing variable, if `SBX_NCTALK_ENABLED=true` but `SBX_NCTALK_URL`, `SBX_NCTALK_USER`, `SBX_NCTALK_TOKEN` or `SBX_NCTALK_ROOM` is absent. Slack and Talk may be enabled at the same time.

## Creating a Sandbox

```bash
./docker-sbx-create-sandbox.sh -n <sandbox-name> -p <absolute-project-path> [-e <env-file>] [-s true|false] [-v <path>...] [-profile-policy <names>] [-profile-directory <names>] [-profiles-dir <path>]
```

- `-n` / `-name` sandbox name (mandatory)
- `-p` / `-project` absolute path to the project directory (mandatory)
- `-e` / `-env` path to an environment file passed into the sandbox (optional)
- `-s` / `-scan-secrets` whether to scan for secrets, default `true` (optional)
- `-protected-paths` whether to install the Claude Code protected-path guard, default `true` (optional; accepts `-protected-paths=false`)
- `-v` / `-volume` absolute path to an extra host directory or file to mount into the sandbox (optional, repeatable). The mount is **read-only by default**; append `:rw` to mount read-write (or `:ro` to be explicit). Each path is mounted inside the sandbox at the same absolute path it has on the host.
- `-profile-policy` flat policy profiles to apply, in the order given (optional). Comma-separate or repeat. A directory bundle named here is refused with a message naming `-profile-directory`.
- `-profile-directory` directory profile bundles to apply, in the order given (optional). Comma-separate or repeat. A flat policy named here is refused with a message naming `-profile-policy`. A bundle may install a sandbox kit at creation, e.g. `-profile-directory flutter-sdk`.
- `-profiles-dir` base directory both selectors resolve against (optional; default is `./profiles` next to the script).

Project files will be available inside the sandbox at the same absolute path they have on the host (`sbx` mounts the project there; a Windows `C:\…` path appears as `/c/…`), and that is where the agent starts. Extra volumes passed via `-v` are reachable at their own absolute host paths. There is no `~/workspace` symlink.

A successful creation ends with `Config saved: <path>` and a ready-to-copy
`<script> -n <name> --recreate` line, printed by `main()` after `save_created_config()`
returns the path. Failing to write the config only warns: the sandbox exists and is
usable, so losing its recreate record is not worth failing the run for.

Normal runs do not echo the `sbx` plumbing calls; `sbx create` still shows its own
KITS/SANDBOX/IMAGE/AGENT progress. `--dry-run` keeps the command echoes. `create_sandbox()`
prints the selected policy and directory profile names under its banner.

## Updating a Sandbox

`docker-sbx-apply-profile.py -n NAME (-profile-policy LIST | -profile-directory LIST)`
applies policies and/or directory bundles to an existing
sandbox. It is a separate command, not a mode of the create script, so the two have
unambiguous, focused argument sets; the implementation stays in the create module (exported
as `main_update()`) so profile loading, asset packing and config writing have one definition.

- It never creates and never scans for secrets, and it does not re-run the in-sandbox setup.
  Adding a profile must not disturb the state of a live sandbox.
- The sandbox must already exist. If it is stopped, the readiness probe (`sbx exec`, which
  Docker documents as starting a stopped sandbox) starts it, and the command waits for that
  first start to settle before copying anything in — the same reason creation waits.
- The operation is additive: network rules are added and same-named asset files are
  overwritten, but nothing is removed or revoked. Un-applying a profile is not supported.
- Newly applied names are added to the saved config's `profiles:` list, deduplicated and in
  the order given, and the command prints `Config updated: <path>`. If the sandbox has no
  saved config it warns and records nothing rather than inventing one: the file would not
  contain the `project` and other fields needed to recreate the sandbox.

The config rewrite is line surgery on the `profiles:` block, not a parse-and-re-render,
because the file is documented as hand-editable and a rewrite from the parsed schema would
drop comments and formatting. It appends only the missing names, deduplicated. An inline
flow list (`profiles: [a, b]`) is rewritten into the block form, because that is the only
form the dependency-free parser can read; any other shape it cannot handle falls back to a
parse-and-re-render, which needs PyYAML for flow collections. A changed
`-profile-directory` is recorded in the same pass: without it, a later `--recreate` would
resolve the same profile names against the default `./profiles`.

## Per-sandbox Configuration

Each sandbox gets `<config-dir>/<name>.yaml` after a successful create, unless
`--no-config` is given. `config_file()` also refuses names containing separators or `.`/`..`,
so a sandbox name cannot escape the config directory.

Default directory, chosen by `default_config_dir()`:

| Platform | Path |
|---|---|
| Linux / WSL | `$XDG_CONFIG_HOME/docker-sbx/sandboxes` (or `~/.config/…`) |
| macOS | `~/Library/Application Support/docker-sbx/sandboxes` |
| Windows | `%APPDATA%\docker-sbx\sandboxes` (or `~/AppData/Roaming/…`) |

Schema (`version` is `1`; unknown keys are rejected):

```yaml
version: 1
name: test01
project: /home/me/projects/test01
env_file: /home/me/private/.anthropic.api.env
scan_secrets: true
protected_paths: true
secrets_settings: /home/me/private/secrets.json
profiles:
  - python
  - node
profile_directories:
  - flutter-sdk
profiles_dir: /home/me/my-profiles
volumes:
  - /mnt/c/docs
  - /mnt/c/data:rw
```

- Only paths, never credentials. `project`, `env_file` and `secrets_settings` are absolute
  when written and are validated as absolute on load — a relative path would silently
  resolve against whatever directory the recreate ran from.
- `profiles` holds flat policy names; `profile_directories` holds directory bundle names.
  They are separate because the CLI selectors are separate.
- `profiles_dir` is omitted when the default `./profiles` is in use, so the file stays
  portable between checkouts; an explicit or loaded value is stored absolutely. The legacy
  `profile_directory` key is accepted on load and rewritten as `profiles_dir` on update.
- A pre-split config with a directory bundle under `profiles:` is migrated in memory on
  load (`_migrate_config_profile_kinds`), with a warning; the CLI flags stay strict.
- `volumes` keeps each spec as passed, including the optional `[:ro|:rw]`.
- `render_config()` emits the documented block subset in a stable key order; the file is
  written atomically (temp file + `os.replace`) and mode `600`, because a crash mid-write
  would otherwise leave a config that cannot be loaded.
- The loader uses PyYAML when importable and the minimal parser otherwise, then
  `validate_config()` normalises types (notably `scan_secrets` and `protected_paths` to
  `"true"`/`"false"`).

`--recreate -n NAME` loads the config named by `-n`, checks it describes that same sandbox,
and then runs the normal creation flow, including the secret scan. It refuses if the sandbox
still exists; `--force` is the explicit opt-in to `sbx rm` first, for the same reason create
refuses to reuse a name. `--from-config FILE` does the same from an arbitrary config and is
mutually exclusive with the creation flags, since the config is the single source for all
of them. A successful recreate writes the config again, so a hand-edited file stays the
record of what was built.

## Policy Profiles

A profile describes additional **per-sandbox** network policies and, optionally, assets
copied into the sandbox. It is either a flat `<name>.yaml` file (network rules only) or a
self-contained directory `<name>/` holding a `profile.yaml`, an asset tree and, optionally,
a sandbox kit. Profiles are applied after all standard rules, so profile rules take
precedence over standard rules. Defining one name in both forms is an error: a silent
precedence rule would make the profile that was applied depend on lookup order.

A directory profile may declare `kit: <dir>`; `<dir>` is a single directory name inside the
bundle and is passed to `sbx create --kit` at creation, so the toolchain is built into the
image. The kit directory is excluded from the Claude asset plan. Kits are create-time only:
`docker-sbx-apply-profile.py` refuses a kit-bearing profile rather than applying its network
rules/assets and silently omitting the toolchain. `profiles/flutter-sdk/` is the shipped
example (`kit: flutter`), and `profiles/flutter.yaml` stays network-only.

### Profile format

```yaml
name: python
description: Python package development
policies:
  network:
    allow:
      - pypi.org
      - files.pythonhosted.org
      - test.pypi.org
    deny:
      - bad.example.com
```

Rules:

- Top-level keys allowed: `name`, `description`, `policies`, `kit`; any other key is
  rejected rather than ignored, so a typo cannot silently produce a profile with no rules.
- `kit` is a single directory name inside a directory profile (no absolute path, `..` or
  separator); the directory must contain a kit descriptor (`*.yaml`). It is refused in a
  flat profile because there is no bundle to resolve it against.
- `policies.network` may contain only `allow` and `deny`, each a list of non-empty domain
  strings. An entry must look like a host (`example.com`, `*.example.com`); a URL, a path
  or whitespace is refused.
- Any key/section that would imply a global policy is rejected; profiles are always per-sandbox.
- Malformed or unknown profiles abort sandbox creation, before `sbx create`.

### Profile assets

A directory profile may carry files in addition to `profile.yaml`:

| Entry | Installed to |
|---|---|
| the declared `kit:` directory | – (build context for `sbx create --kit`, never copied) |
| `agent/` | `/home/agent/.claude/` |
| `project/` | `<project>/.claude/` |
| any other root entry (`skills/`, `agents/`, `commands/`, `workflows/`, `CLAUDE.md`, …) | `/home/agent/.claude/<entry>` |

Agent level is the default so a bundle author only writes the tree once; `agent/` and
`project/` exist to override that for the entries that must land on the other side.

Delivery is: package the selected files into a gzip tarball on the host, `sbx cp` it into
the guest, unpack it with the guest's `tar`, then delete the archive. Project-level assets
land in `<project>/.claude/`, which is the shared volume, so they become part of the host
working tree and not just the sandbox.

Things to keep in mind when editing this feature:

- Build the tarball with Python's `tarfile`, not the host's `tar`: a `tar` binary is not
  guaranteed on Windows and its flags differ between the GNU and BSD implementations, whereas
  `tarfile` is standard library everywhere. The archive is assembled from an
  already-validated file list. Entries are written with `uid`/`gid` 0, empty owner names and
  `mtime` 0 so the same bundle produces the same archive.
- Asset delivery is **fatal** on failure (`die`), unlike the `sbx policy` calls around it.
  A profile that claims to install a skill must not silently install half of it; a policy
  call that fails is visible on `sbx policy ls`, but a missing asset is not.
- Symlinks and special files are refused while collecting the tree. The destination is the
  agent's home or the shared project volume, and a link can point outside both — and out of
  the volume, where a project-level asset has no business writing.
- Refuse `settings.json`, `settings.local.json` and `.credentials.json` at agent level:
  `sandbox/docker-sbx-setup-sandbox.sh` merges its telemetry settings and notification hooks
  into `settings.json`, so a profile that shipped its own would be overwritten or would
  overwrite docker-sbx's entries depending on order. At project level only `settings.json`
  is allowed, because that file belongs to the project; `.git` and `.credentials.json` are
  still refused.
- The check is on the first path component, so refusing `settings.json` also refuses
  `settings.json/…`; a directory cannot be used to smuggle the same name in deeper.
- Later profiles overwrite earlier same-named files; nothing is deleted, so applying
  profiles is additive.
- Cap the bundle at 2000 files / 32 MiB. This is a rail against pointing at a whole
  checkout by accident, not a considered limit.
- `profiles/example/` is the template users are meant to copy; it shows the optional
  `kit:` key as a comment. Most shipped language profiles stay flat and network-only;
  `profiles/flutter-sdk/` is the directory form with a kit.

### Standard profiles

The `./profiles` directory contains these built-in profiles. Most carry network rules only;
assets and kits require the directory form.

- `python` — PyPI and test PyPI
- `java` — Maven Central and Gradle repositories
- `java-spring-boot` — Java domains plus Spring repositories and `start.spring.io`
- `go` — Go module proxy and common VCS hosts (GitHub, GitLab, Bitbucket)
- `node` — npm and Yarn registries
- `rust` — crates.io
- `dotnet` — NuGet.org
- `flutter` — Flutter/Dart package access plus Android build dependencies (network only)
- `flutter-sdk` — the above plus a sandbox kit installing Flutter, Dart, JDK 17, the
  Android SDK and the Linux desktop toolchain; x86_64 only, no emulator/iOS/web

Example:

```bash
./docker-sbx-create-sandbox.sh -n pyproject -p "$HOME/projects/pyproject" -profile-policy python
./docker-sbx-create-sandbox.sh -n spring -p "$HOME/projects/spring" -profile-policy java-spring-boot
./docker-sbx-create-sandbox.sh -n fullstack -p "$HOME/projects/fullstack" -profile-policy python,node
./docker-sbx-create-sandbox.sh -n mobile -p "$HOME/projects/mobile" -profile-directory flutter-sdk
```

## Environment Files

Example env files to pass via `-e`:

**Anthropic API key** (`.anthropic.api.env`):
```
ANTHROPIC_API_KEY=<key>
```

**Anthropic auth token** (`.anthropic.auth.env`):
```
ANTHROPIC_AUTH_TOKEN=<token>
```

**Local Ollama backend** (`.ollama.srv.env`):
```
ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ANTHROPIC_AUTH_TOKEN=ollama
```

When `ANTHROPIC_BASE_URL` is set, the setup script sets `ANTHROPIC_AUTH_TOKEN`.

## Notifications (optional)

Two providers, **Slack** and **Nextcloud Talk**, share one architecture. Each is off by
default and enabled purely through `-e`; there are no CLI flags. With no `SBX_SLACK_*` and
no `SBX_NCTALK_*` variables present the sandbox is configured exactly as it was before.
They are independent: either can be enabled, or both at once, and enabling one never
affects the other (each has its own `*_ENABLED` master switch, its own `.env` file, and its
own hook script wired separately into `settings.json`). In `sandbox/docker-sbx-setup-sandbox.sh`
the two share `hook_var`, `write_notify_env` and `wire_notify_hooks`, parameterised by
variable prefix; the host side mirrors this with `validate_slack_config`/`validate_nctalk_config`,
the shared `validate_notify_events`, and `update_sbx_policy_slack`/`update_sbx_policy_nctalk`.

### Slack

```
SBX_SLACK_ENABLED=true
SBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T…/B…/xxx   # mode A: webhook
SBX_SLACK_BOT_TOKEN=xoxb-…                                         # mode B: bot token
SBX_SLACK_CHANNEL=C0123456789                                      # mode B: channel id
SBX_SLACK_EVENTS=Stop,Notification,SessionStart,SessionEnd         # default set
SBX_SLACK_PREFIX=[docker-sbx]                                      # optional, prepended
SBX_SLACK_DRYRUN=true                                              # optional, print instead of post
```

- Both styles post as a **Slack app**, never as the user: the app must be declared in the workspace and given a destination it may post to (the channel selected when creating the webhook, or one the app was invited into — an app belongs to no channel by default). The scripts only consume the credential; they provision nothing in Slack, so `channel_not_found` and `not_in_channel` are the expected failures when the destination side is not set up. A member's own DM with themselves is a valid-looking `D…` id that no app can post into — `README.md` documents both.
- Bot token wins when both styles are configured.
- Per-event target override: append the upper-cased event name, e.g. `SBX_SLACK_WEBHOOK_NOTIFICATION` or `SBX_SLACK_CHANNEL_NOTIFICATION`.
- Accepted events: `Stop`, `Notification`, `SessionStart`, `SessionEnd`, `SubagentStop`, `UserPromptSubmit`. An unknown name aborts creation.
- Message format: `[<sandbox>] <project> — <phrase>`, plus Claude Code's own reason on a second line for `Notification` events.

What gets installed inside the sandbox:

- `~/.claude/hooks/slack-notify.sh` (755) — the hook itself; always exits 0 and never writes to stdout.
- `~/.claude/hooks/slack.env` (600) — credentials plus the sandbox and project labels.
- `~/.claude/hooks/slack-errors.log` (600) — one line per failed post, created on the first failure. Not created when everything succeeds.
- `hooks` entries in `~/.claude/settings.json` for the chosen events, merged idempotently: docker-sbx's own entries are replaced and any other hooks are preserved. `matcher` is deliberately omitted — these are lifecycle events, not tool events.

Things to keep in mind when editing this feature:

- The hook sits on the critical path of every enabled event: always exit 0, never write to stdout (dry-run diagnostics go to stderr), and cap network calls with `curl --max-time`.
- Failures are recorded, not swallowed. A post that never lands is silent on the agent's side, so before `slack-errors.log` existed a revoked webhook, a bot that was never invited and a request blocked by the network policy all looked identical to success. Keep the distinction: Slack reports API errors (`not_in_channel`, `invalid_auth`, `missing_scope`) as **HTTP 200 with `ok:false`**, so the response body — not the status code — is what decides success in bot-token mode. The log is truncated past 64 KB and never contains a webhook path (`log_safe_url` strips it, since the path is the credential). Bot-token failures also name the channel that was tried: `channel_not_found` and `not_in_channel` are only distinguishable by seeing what was actually sent — a channel id, a `#name`, or a value that still carried CRLF are indistinguishable from Slack's reply alone.
- The POST must declare `Content-type: application/json`. curl's default for `--data-binary` is `application/x-www-form-urlencoded`, and Slack — told to expect form data but handed JSON — answers HTTP 200 `{"ok":false,"error":"invalid_form_data"}` rather than a 4xx. The header is therefore set once inside `post_request`, not at each call site, and `run-tests.sh` asserts it on the wire: a sink that merely reads the body cannot tell a good request from this broken one, which is how the header was once lost in a refactor without any test noticing.
- Bot tokens and secret webhook URLs are passed through `curl --config -` on stdin, not through curl's argument list, so they do not appear in `ps`. `run-tests.sh` checks both the argv and the stdin configuration with a stub, while the loopback sink verifies the request on the wire.
- The two halves must agree on how the env file is parsed. `env_file_get` (host) trims surrounding whitespace; `load_env_file` (sandbox) must strip at least a trailing CR, because a CRLF env file — the default from a Windows editor on WSL — otherwise yields `true\r`, which is not `true`. When they disagree the host reports the feature as enabled while the sandbox installs nothing, so `install_slack_hook`/`install_nctalk_hook` also warn loudly rather than no-op'ing when their `*_ENABLED` is set to anything unrecognised. Both guards are covered by `run-tests.sh`.
- `slack.env` is **sourced** by the hook, so values are written as single-quoted shell literals — a project path containing quotes, `<`, `&` or `$(…)` has to stay inert.
- Escape `&`, `<`, `>` before posting: Slack treats a bare `<` as the start of a link/mention entity. Use `sed` rather than `${var//pat/repl}` — from bash 5.2 (Ubuntu 24.04+) `patsub_replacement` is on by default, so `&` in the replacement means "the matched text" and turns `&lt;` into `<lt;`.
- `env_file_get` in `docker-sbx-create-sandbox.sh` ends with `|| true` on purpose: that script runs with `set -o pipefail`, so an absent key would otherwise abort creation through `set -e`.
- The mechanism is verified, not assumed: a settings-level `hooks` block does fire, `matcher` is optional for lifecycle events, and the payload arrives on stdin. `run-tests.sh` covers the env contract (including a CRLF env file), the merge, failure logging, and a real POST to a loopback endpoint.

### Nextcloud Talk

All variables carry the `SBX_NCTALK_` prefix; `SBX_NCTALK_ENABLED` is the master switch. It
posts through the Talk **spreed** OCS API, so nothing is provisioned for the operator: the
Nextcloud user, its app password and the room must already exist.

```
SBX_NCTALK_ENABLED=true
SBX_NCTALK_URL=https://cloud.example.com           # Nextcloud base URL
SBX_NCTALK_USER=agent                              # user the app password belongs to
SBX_NCTALK_TOKEN=xxxxx-xxxxx-xxxxx-xxxxx-xxxxx     # app password (not the login password)
SBX_NCTALK_ROOM=abc123XYZ                          # Talk room token (…/call/abc123XYZ)
SBX_NCTALK_EVENTS=Stop,Notification,SessionStart,SessionEnd
SBX_NCTALK_PREFIX=[docker-sbx]                     # optional, prepended
SBX_NCTALK_DRYRUN=true                             # optional, print instead of post
```

- All four of URL/USER/TOKEN/ROOM are required when enabled; creation aborts and names each
  missing one. Per-event room override: append the upper-cased event name, e.g.
  `SBX_NCTALK_ROOM_NOTIFICATION`. Same accepted events and same message format as Slack.
- The endpoint is `<SBX_NCTALK_URL>/ocs/v2.php/apps/spreed/api/v1/chat/<room>`, with the app
  password as HTTP basic auth, `Content-Type: application/json`, and `OCS-APIRequest: true`.
- What gets installed: `~/.claude/hooks/nctalk-notify.sh` (755), `~/.claude/hooks/nctalk.env`
  (600), and `~/.claude/hooks/nctalk-errors.log` (600, one line per failed post, created on
  the first failure). Its `hooks` entries are merged alongside any Slack entries — the merge
  matches on the exact command path, so wiring one provider never disturbs the other.
- The host side allows the Nextcloud instance's host in the per-sandbox policy and nothing
  more.

Things to keep in mind when editing the Talk provider:

- It mirrors the Slack hook deliberately; keep the two in step. A change to the message
  format, the event set, the env-file parsing or the failure-logging shape should be made in
  both, and `run-tests.sh` runs the same assertions against each.
- Nextcloud Talk renders messages as Markdown and has no link/mention entity, so unlike
  Slack the text is **not** HTML-escaped — do not "port" `escape_slack` across.
- An OCS reply carries its own `ocs.meta.statuscode`, but `v2.php` mirrors it in the HTTP
  status, so success is decided by a `2xx` status (as for the Slack webhook mode); a non-2xx
  is logged with its status, the room and the response body. The room token (a destination
  id) is logged like Slack's channel id, while `log_safe_url` keeps the path out of the line.
- The app password is passed to curl as basic auth through `curl --config -` on stdin, not
  through `-u` on its command line, so it does not appear in the process argument list.

## Using a Sandbox

- Start: `sbx run <sandbox-name>`
- Interactive shell: `sbx exec -ti <sandbox-name> bash`
- Manage policies: `sbx policy allow network <sandbox-name> <domain>`

## Claude Code Settings Written by Setup

`sandbox/docker-sbx-setup-sandbox.sh` writes these opt-outs into `~/.claude/settings.json` inside the sandbox:

- `DISABLE_TELEMETRY=1`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
- `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1` (redundant with the two above, kept for what it says)

It does **not** write `CLAUDE_CODE_ENABLE_TELEMETRY` (that variable turns OpenTelemetry export *on* with `1`; export is already off unless it is set, so a `0` said nothing) or `CLAUDE_CODE_DISABLE_TELEMETRY` (not a Claude Code variable — `DISABLE_TELEMETRY` is the real name, so the key was never read). It also no longer sets `CLAUDE_CODE_ATTRIBUTION_HEADER`: the `0` was a workaround for claude before 2.1.181, where the attribution block carried a per-request token on custom base URLs, and the template ships 2.1.280 (a newer version is downloaded at first start). A value supplied in the `-e` env file is still passed through.

Optionally injects `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL`.
