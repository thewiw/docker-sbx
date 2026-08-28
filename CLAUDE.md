# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains bash scripts that install the [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine and automate creation of AI agent sandboxes using Claude Code. Each sandbox is an isolated Docker container with a shared project volume and a restrictive network policy.

Tested on WSL Ubuntu 24.04/26.04 LTS and native Ubuntu 26.04 LTS.

## Scripts

All scripts must remain in the same directory and are run from that directory.

| Script / Directory | Purpose |
|--------|---------|
| `docker-sbx-install.sh` | One-time host setup. Installs Docker CE and the `docker-sbx` engine, creates a `kvm` group user, and sets default network policies (deny-all, allow only Ubuntu archives and Docker download). Must be run with `sudo`. |
| `docker-sbx-create-sandbox.sh` | Creates a new sandbox for a project. Scans the project for secrets (file patterns + gitleaks), runs `sbx create`, then copies `docker-sbx-setup-sandbox.sh` into the sandbox and executes it. Optionally applies per-sandbox policy profiles. |
| `docker-sbx-setup-sandbox.sh` | Runs **inside** the sandbox. Stops any running `apt` processes, updates packages, installs `jq`, writes Claude Code telemetry/opt-out settings into `~/.claude/settings.json`, and symlinks `~/workspace` to the project path on the host. |
| `profiles/` | YAML policy profiles used by `docker-sbx-create-sandbox.sh` to grant a sandbox access to language-specific external resources. |

## Security Model

- **Default network policy**: deny-all. Only `archive.ubuntu.com`, `security.ubuntu.com`, and `download.docker.com` are allowed globally. Explicit deny rules are also set for GitHub, GitLab, Bitbucket, and Postman domains.
- **Secret scanning**: before a sandbox is created, the script checks for credential files (`google-services.json`, `key.properties`, `*.jks`, `*.cert`, `*.crt`, `*.key`) and runs `gitleaks` against the project's git repository. Creation aborts if secrets are found.
- **Shared volume**: the project directory is mounted as a shared volume. Claude Code has write access to the entire sandbox filesystem, including this volume. Do not place credentials inside the project path or in git history.

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

Project files will be available at `~/workspace` inside the sandbox. Extra volumes passed via `-v` are reachable at their own absolute host paths.

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

When `ANTHROPIC_BASE_URL` is set, the setup script disables the attribution header and sets `ANTHROPIC_AUTH_TOKEN`.

## Using a Sandbox

- Start: `sbx run <sandbox-name>`
- Interactive shell: `sbx exec -ti <sandbox-name> bash`
- Manage policies: `sbx policy allow network <sandbox-name> <domain>`

## Claude Code Settings Written by Setup

`docker-sbx-setup-sandbox.sh` writes the following to `~/.claude/settings.json` inside the sandbox:

- `DISABLE_TELEMETRY=1`
- `CLAUDE_CODE_ENABLE_TELEMETRY=0`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
- `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1`
- `CLAUDE_CODE_ATTRIBUTION_HEADER=1` (or `0` when using a custom base URL)

Optionally injects `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL`.
