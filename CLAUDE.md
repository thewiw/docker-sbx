# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains scripts that install the [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine and automate creation of AI agent sandboxes using Claude Code. Each sandbox is an isolated Docker container with a shared project volume and a restrictive network policy.

The bash scripts are tested on WSL Ubuntu 24.04/26.04 LTS and native Ubuntu 26.04 LTS. The host scripts are being migrated to cross-platform Python (Linux/WSL, macOS, Windows).

## Scripts

Scripts that run on the **host** live in the repository root and are run from there. Scripts that run **inside** the sandbox live in `sandbox/` and are staged into the sandbox by `docker-sbx-create-sandbox.sh`; they are never run on the host. Host scripts are being migrated from bash to cross-platform Python (`*.py`, Python 3.9+, standard library only); the `.sh` files are kept until the ports are validated. Test suites live in `test/`.

| Script / Directory | Purpose |
|--------|---------|
| `docker-sbx-install.sh` | One-time host setup. Installs Docker CE and the `docker-sbx` engine, creates a `kvm` group user, and sets default network policies (deny-all, allow only Ubuntu archives and Docker download). Must be run with `sudo`. |
| `docker-sbx-install.py` | Cross-platform replacement for `docker-sbx-install.sh`, runnable on Linux/WSL, macOS and Windows (Python 3.9+, standard library only). Does the same setup: installs or updates Docker, installs the `sbx` CLI, signs in, sets the global network policy, and persists the environment variables. `--dry-run` prints every command and changes nothing, and needs no privilege on any platform. Needs `sudo` on Linux; runs as a regular user on Windows and macOS (Homebrew refuses to run as root). The only Windows step needing administrator rights — enabling the Windows Hypervisor Platform — is probed first and demanded only when the feature is missing. |
| `docker-sbx-create-sandbox.sh` | Creates a new sandbox for a project. Scans the project for secrets (file patterns + gitleaks), runs `sbx create`, then copies `sandbox/docker-sbx-setup-sandbox.sh` into the sandbox and executes it. Optionally applies per-sandbox policy profiles. |
| `docker-sbx-create-sandbox.py` | Cross-platform replacement for `docker-sbx-create-sandbox.sh` (Linux/WSL, macOS, Windows; Python 3.9+, standard library only). Same options and the same user-facing messages; `--dry-run` prints every command and creates nothing. Profiles are YAML: PyYAML is used when importable, otherwise a small parser covers the documented profile subset. It waits for the new sandbox's first start to finish before configuring it, refuses to reuse an existing sandbox name, and treats a failed `sbx create` as fatal — the bash version's `sbx()` calls are non-fatal (mirroring the original), so a failed create used to be followed by setup against the *previous* sandbox of that name. |
| `sandbox/docker-sbx-setup-sandbox.sh` | Runs **inside** the sandbox, after the host has waited for the sandbox's first start to finish. Takes the project path and an optional env-file path (or `-`). Installs `jq` **only if it is missing** — that is the only `apt` use, and it never kills `apt`/`dpkg` or upgrades packages, because the sandbox runs its own `apt` at boot (`/etc/durable-startup.d/001-startup-claude/001-cmd.sh`) and is still installing its agent during that first start. Writes Claude Code telemetry/opt-out settings into `~/.claude/settings.json`. It deliberately does **not** create a `~/workspace` symlink: the project is already mounted at its host path. Also installs the optional Slack notification hook when the env file asks for it. |
| `sandbox/docker-sbx-slack-notify.sh` | Runs **inside** the sandbox as `~/.claude/hooks/slack-notify.sh`. Invoked by Claude Code as a hook with the event payload on stdin; posts one short line per lifecycle event to Slack. Only installed when the optional feature is enabled via the env file. |
| `docker-sbx-update-sandboxes.sh` | Maintenance helper. Runs `apt update/upgrade/autoremove` inside existing sandboxes via `sbx exec`, one at a time. Enumerates with `sbx ls` (or a custom `SBX_LS_CMD`), selectable with `-i`/`-x`; `DRY_RUN=1` prints instead of running. Not part of sandbox creation. |
| `docker-sbx-update-sandboxes.py` | Cross-platform replacement for `docker-sbx-update-sandboxes.sh` (Linux/WSL, macOS, Windows; Python 3.9+, standard library only). Same options, the same user-facing messages and the same exit codes (0 clean, 1 when a sandbox fails, 2 on bad usage); adds a `--dry-run` flag alongside `DRY_RUN=1`. Lists sandboxes from `SBX_LS_CMD`, then `sbx ls -q`, then `sbx ls --json`, then the human-readable table — the JSON branch walks the parsed document itself, so `jq` is no longer needed. `SBX_LS_CMD` runs through the platform shell (`sh -c`, or `cmd /c` on Windows); the `sh -c` handed to `sbx exec` stays `sh -c`, since that one runs inside the sandbox. |
| `test/run-tests.sh` | Test suite for the Slack notification feature, the env-file contract and the `create-sandbox` CLI. Exercises the hook, the settings merge, failure logging, and a real POST to a loopback sink. The CLI-level tests (1-12) run against **both** the bash and Python implementations, so the two cannot drift apart; `SBX_IMPLS=python` restricts it to one. Run directly on the host (`bash test/run-tests.sh`); no sandbox required. |
| `test/test_docker_sbx_install.py` | Unit tests for the pure helpers in `docker-sbx-install.py` (prompt parsing, platform/locale selection, profile path, environment-block idempotency). Run with `python3 -m unittest discover -s test -p 'test_*.py'`. |
| `test/test_create_sandbox.py` | Unit tests for the pure helpers in `docker-sbx-create-sandbox.py` (argument parsing, env-file reading, volume specs, Slack validation, host extraction, the secrets-file scanner, the minimal YAML profile parser, the sandbox-name pre-check and the wait for a sandbox's first start). Same command as above. |
| `test/test_update_sandboxes.py` | Unit tests for the pure helpers in `docker-sbx-update-sandboxes.py` (list normalisation, CLI parsing and its exit-2 messages, the apt chain, exec-flag detection, and the three sandbox-name extraction paths). Same command as above; needs no `sbx` and no sandbox. |
| `profiles/` | YAML policy profiles used by `docker-sbx-create-sandbox.sh` to grant a sandbox access to language-specific external resources. |

## Security Model

- **Default network policy**: deny-all. Only `archive.ubuntu.com`, `security.ubuntu.com`, and `download.docker.com` are allowed globally. Explicit deny rules are also set for GitHub, GitLab, Bitbucket, and Postman domains.
- **Secret scanning**: before a sandbox is created, the script checks for credential files (`google-services.json`, `key.properties`, `*.jks`, `*.cert`, `*.crt`, `*.key`) and runs `gitleaks` against the project's git repository. Creation aborts if secrets are found.
- **Shared volume**: the project directory is mounted as a shared volume. Claude Code has write access to the entire sandbox filesystem, including this volume. Do not place credentials inside the project path or in git history.
- **Slack notifications** (optional): when enabled, the sandbox's own network policy is extended with just the endpoint the configured credential style needs (`hooks.slack.com` for a webhook, `slack.com` for a bot token) — no global rule is added. The credential is written to `~/.claude/hooks/slack.env`, mode `600`, outside the project volume. It is still readable by the agent in that sandbox, so scope the credential to what you are willing to expose there: an incoming webhook bound to a single channel is least privilege, a bot token (`chat:write`) can post anywhere the bot is a member. `docker-sbx-create-sandbox.sh` aborts if `SBX_SLACK_ENABLED=true` but no usable target is configured, rather than silently installing a hook that can never deliver.

## Creating a Sandbox

```bash
./docker-sbx-create-sandbox.sh -n <sandbox-name> -p <absolute-project-path> [-e <env-file>] [-s true|false] [-v <path>...] [-profile <names>] [-profile-directory <path>]
```

- `-n` sandbox name (mandatory)
- `-p` absolute path to the project directory (mandatory)
- `-e` path to an environment file passed into the sandbox (optional)
- `-s` whether to scan for secrets, default `true` (optional)
- `-v` absolute path to an extra host directory or file to mount into the sandbox (optional, repeatable). The mount is **read-only by default**; append `:rw` to mount read-write (or `:ro` to be explicit). Each path is mounted inside the sandbox at the same absolute path it has on the host.
- `-profile` comma-separated list of policy profile names to apply, e.g. `python` or `python,java` (optional). Profiles are applied in the order given.
- `-profile-directory` path to a directory containing profile YAML files (optional; default is `./profiles` next to the script).

Project files will be available inside the sandbox at the same absolute path they have on the host (`sbx` mounts the project there; a Windows `C:\…` path appears as `/c/…`), and that is where the agent starts. Extra volumes passed via `-v` are reachable at their own absolute host paths. There is no `~/workspace` symlink.

## Policy Profiles

Profiles are YAML files that describe additional **per-sandbox** network policies.
They are applied after all standard rules, so profile rules take precedence over
standard rules.

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

- Top-level keys allowed: `name`, `description`, `policies`.
- `policies.network` may contain `allow` and `deny`, each a list of non-empty domain strings.
- Any key/section that would imply a global policy is rejected; profiles are always per-sandbox.
- Malformed or unknown profiles abort sandbox creation.

### Standard profiles

The `./profiles` directory contains these built-in profiles:

- `python` — PyPI and test PyPI
- `java` — Maven Central and Gradle repositories
- `java-spring-boot` — Java domains plus Spring repositories and `start.spring.io`
- `go` — Go module proxy and common VCS hosts (GitHub, GitLab, Bitbucket)
- `node` — npm and Yarn registries
- `rust` — crates.io
- `dotnet` — NuGet.org
- `flutter` — pub.dev and Google Storage for Flutter SDK / Dart packages

Example:

```bash
./docker-sbx-create-sandbox.sh -n pyproject -p "$HOME/projects/pyproject" -profile python
./docker-sbx-create-sandbox.sh -n spring -p "$HOME/projects/spring" -profile java-spring-boot
./docker-sbx-create-sandbox.sh -n fullstack -p "$HOME/projects/fullstack" -profile python,node
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

## Slack Notifications (optional)

Off by default, enabled purely through `-e`; there are no CLI flags. With no `SBX_SLACK_*`
variables present the sandbox is configured exactly as it was before.

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

- The hook sits on the critical path of every enabled event: always exit 0, print nothing, and cap network calls with `curl --max-time`.
- Failures are recorded, not swallowed. A post that never lands is silent on the agent's side, so before `slack-errors.log` existed a revoked webhook, a bot that was never invited and a request blocked by the network policy all looked identical to success. Keep the distinction: Slack reports API errors (`not_in_channel`, `invalid_auth`, `missing_scope`) as **HTTP 200 with `ok:false`**, so the response body — not the status code — is what decides success in bot-token mode. The log is truncated past 64 KB and never contains a webhook path (`log_safe_url` strips it, since the path is the credential). Bot-token failures also name the channel that was tried: `channel_not_found` and `not_in_channel` are only distinguishable by seeing what was actually sent — a channel id, a `#name`, or a value that still carried CRLF are indistinguishable from Slack's reply alone.
- The POST must declare `Content-type: application/json`. curl's default for `--data-binary` is `application/x-www-form-urlencoded`, and Slack — told to expect form data but handed JSON — answers HTTP 200 `{"ok":false,"error":"invalid_form_data"}` rather than a 4xx. The header is therefore set once inside `post_request`, not at each call site, and `run-tests.sh` asserts it on the wire: a sink that merely reads the body cannot tell a good request from this broken one, which is how the header was once lost in a refactor without any test noticing.
- The two halves must agree on how the env file is parsed. `env_file_get` (host) trims surrounding whitespace; `load_env_file` (sandbox) must strip at least a trailing CR, because a CRLF env file — the default from a Windows editor on WSL — otherwise yields `true\r`, which is not `true`. When they disagree the host reports the feature as enabled while the sandbox installs nothing, so `install_slack_hook` also warns loudly rather than no-op'ing when `SBX_SLACK_ENABLED` is set to anything unrecognised. Both guards are covered by `run-tests.sh`.
- `slack.env` is **sourced** by the hook, so values are written as single-quoted shell literals — a project path containing quotes, `<`, `&` or `$(…)` has to stay inert.
- Escape `&`, `<`, `>` before posting: Slack treats a bare `<` as the start of a link/mention entity. Use `sed` rather than `${var//pat/repl}` — from bash 5.2 (Ubuntu 24.04+) `patsub_replacement` is on by default, so `&` in the replacement means "the matched text" and turns `&lt;` into `<lt;`.
- `env_file_get` in `docker-sbx-create-sandbox.sh` ends with `|| true` on purpose: that script runs with `set -o pipefail`, so an absent key would otherwise abort creation through `set -e`.
- The mechanism is verified, not assumed: a settings-level `hooks` block does fire, `matcher` is optional for lifecycle events, and the payload arrives on stdin. `run-tests.sh` covers the env contract (including a CRLF env file), the merge, failure logging, and a real POST to a loopback endpoint.

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
