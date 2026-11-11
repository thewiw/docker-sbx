# Docker SBX

Tools to install [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine + create and manage AI Agent sandboxes

Work in progress !!!
Use at your own risk !!!

Tested on :

  - WSL Ubuntu 24.04 LTS and Ubuntu 26.04 LTS

  - Ubuntu 26.04 LTS

  - Windows 11

Host scripts (install / create / update / test) live in the repository root and must be run
from that directory. Scripts that run **inside** a sandbox live in `sandbox/`; they are staged
in automatically by `docker-sbx-create-sandbox.py` and are not run on the host.


## Install engine

This must be done only once.

First [create a docker account](https://app.docker.com/signup/) if you do not have one already or if you want one for this installation.

The cross-platform installer is `docker-sbx-install.py` (Python 3.9+, standard library only). It
runs on Linux, WSL2, macOS and Windows, and whichever platform you are on it installs or updates
Docker, installs the `sbx` CLI, signs you in (at one point you will need your docker account),
sets the global network policy, and persists the environment variables. Add `--dry-run` to print
every command without changing anything; it needs no special privilege on any platform.

At the end of installation, all network communication from the sandboxes is prohibited, except for system updates.

### Linux (including WSL2)

Run it with `sudo` — the Linux path installs system packages and Docker itself:

    sudo python3 docker-sbx-install.py

What is specific to Linux:

- **Docker Engine**, not Docker Desktop, is installed from Docker's own `apt` repository, together
  with the `docker-sbx` package, `util-linux-extra` and `python3-yaml`.
- The invoking user is added to the **`kvm` group**, which local sandboxes need for hardware
  virtualization. Group membership only applies to new sessions, so for the commands that run
  during installation the script re-enters the session through `sg kvm`; your own shells pick it
  up the next time you log in.
- **WSL2** is detected and handled as Linux, so the `sudo` invocation and the `kvm` group are the
  same as above. Two things to keep in mind there: a Windows-side project is reached under
  `/mnt/c/...` rather than a native path, and the sandbox relies on WSL2's nested virtualization
  for KVM.

### Windows

Run it from a **regular user** terminal; no elevated prompt is needed:

    python  docker-sbx-install.py

What is specific to Windows:

- Tested on **Windows 11**.
- The installer uses **`winget`** for both Docker Desktop (`Docker.DockerDesktop`) and the `sbx`
  CLI (`Docker.sbx`), so a healthy winget is a prerequisite — see the troubleshooting below.
- **Docker Desktop needs WSL2 or Hyper-V enabled**, and a reboot afterwards.
- Docker Desktop's and `sbx`'s own installers raise their own **UAC** prompt when they need one, so
  the install itself never needs an elevated terminal. The one step that does is enabling the
  **Windows Hypervisor Platform** below.
- A Windows `C:\...` project path is visible inside the sandbox as `/c/...`.

**Windows Hypervisor Platform** is needed by **local** sandboxes (cloud sandboxes do not need it).
It is *not* Hyper-V, and *not* the Virtual Machine Platform that WSL 2 uses — having either of those
does not turn it on, so a machine that "already has the hypervisor" can still need this step.
`Enable-WindowsOptionalFeature` cannot prompt for itself, so this is the one step that requires an
elevated terminal; the installer reads the feature's state first and asks you to run one command as
administrator only when it is actually off. (Docker Desktop has no need for this feature and does
not enable it.) To check and enable it yourself, in an **elevated** PowerShell:

```powershell
(Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform).State
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
```

Restart Windows if prompted. `EnablePending` in the first command's output means it is on but
waiting for that restart.

Reading the state needs administrator rights too — for a standard user
`Get-WindowsOptionalFeature -Online` fails with *Access is denied*, which makes an enabled
feature look missing. The script therefore reads it through `Win32_OptionalFeature`, which
standard users can query, and it will not block the install if that read fails: it warns and
tells you how to check by hand instead.

#### Windows: winget fails with `0x8a15000f`

On Windows the installer uses `winget` both for Docker Desktop and for the `sbx` CLI. If
`winget` itself is unhealthy you get, right after answering `y` to the Docker Desktop prompt:

    Échec lors de la recherche de la source : winget
    0x8a15000f : Il manque des données requises par la source
    Aucun package n'a été trouvé parmi les sources de travail.

`0x8a15000f` is `APPINSTALLER_CLI_ERROR_SOURCE_DATA_MISSING`: winget's **source catalog cannot be
loaded**, so it reports "no package found" for a package that does exist. This is a winget-side
problem, not a script problem — no installer option works around it. Fix the source first, in an
**elevated** (Administrator) PowerShell:

    winget source reset --force
    winget source update
    winget source list

`source reset --force` rebuilds the stale local index; `source list` should then show `msstore`
and `winget`. Retry `winget install Docker.DockerDesktop`, or simply re-run the installer.

If `source list` is still empty or errors, the *App Installer* package is the broken part: repair
it from **Settings → Apps → App Installer → Advanced options → Repair**, then **Reset** if Repair
does not help, or reinstall *App Installer* from the Microsoft Store. Check with `winget --version`
before retrying.

Failing that, install Docker Desktop by hand from
<https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe>. Docker Desktop needs
WSL2 or Hyper-V enabled, and a reboot afterwards.

**Close the terminal and open a new one** before re-running the installer. The Docker Desktop
installer adds the `docker` command to PATH, but that change only reaches terminals started
afterwards — a shell that was already open keeps the environment it inherited, so `docker` still
resolves to nothing there and the installer would offer to install Docker again. In a fresh
terminal it detects the existing Docker and skips the winget step.

Get winget healthy **before** re-running the whole script: the `sbx` CLI is installed through the
same source, so it fails with the identical error a few steps later.

### macOS

Run it as a **regular user** — Homebrew refuses to run as root:

    python3 docker-sbx-install.py

What is specific to macOS:

- **Homebrew is required.** Docker Desktop is installed with `brew install --cask docker`, and the
  `sbx` CLI with `brew trust docker/tap` followed by `brew install docker/tap/sbx`.
- There is no native Docker Engine on macOS, so **Docker Desktop provides the `docker` daemon**; it
  is needed both for local sandboxes and for the host-side secret scan (gitleaks runs in a
  container, see [Create a sandbox](#create-a-sandbox)).


## Create a sandbox

This must be done each time a new sandbox must be created.

Each sandbox is based on 3 main components :
  - Claude Code as AI Agent
  - a shared volume where the project is
  - a network security policy (a restrictive default shared by all sandboxes — only HTTP, HTTPS and SSH protocols are allowed — which per-sandbox profiles can extend)

Claude Code has got write access to the whole sandbox filesystem, including the shared volume, so DO NOT STORE CREDENTIALS in this project volume

For maximum security, the shared volume should only contain project files (sources, git, ...) WITHOUT any credentials/certificates/... files.

For the same reason, sources history (git) MUST NOT contain credentials/certificates/... either (or worst-case scenario if those data exist then they must be obsolete).

The cross-platform Python version (Linux, WSL, macOS, Windows) takes exactly the same options:

    python3 docker-sbx-create-sandbox.py -n test01 -p /absolute/project/path

Add `--dry-run` to print every command without creating anything.

```
python3 docker-sbx-create-sandbox.py -n [sandbox name] -p [absolute path] -e {path to env file} -s {true/false} -sf {path to secrets files settings} -gsf {path to secrets files settings} -v {path} -profile {comma-separated profile names} -profile-directory {path to profile directory} :
  -n                  : name of the sandbox [[mandatory]]
  -p                  : absolute path of the project's files [[mandatory]]
  -e                  : path to environment file [[optional]]
  -s                  : check secrets [[optional, true by default]]
  -sf                 : path to secrets files settings [[optional, uses default settings if missing]]
  -gsf                : generate a default secrets files settings and exit, parameter is path to secrets files settings [[optional]]
  -v                  : extra host directory/file to mount into the sandbox (absolute path) [[optional, repeatable]]
  -profile            : comma-separated list of policy profiles to apply (e.g. python,java) [[optional]]
  -profile-directory  : directory containing profile YAML files [[optional, default: ./profiles]]
```

The setup step **waits for the sandbox's first start to finish** before configuring it. That first
start is when the guest gets its agent — the `claude` binary is downloaded then, so it can take a
while — and when the sandbox runs its own `apt` hooks. Touching the sandbox in the middle of that
window is what used to leave sandboxes that could not be started again, so `docker-sbx` waits for
it and then configures the sandbox.

Nothing `docker-sbx` does inside a sandbox kills `apt`/`dpkg` or runs `apt upgrade`: only `jq` is
installed, and only when it is actually missing (the template ships it). To refresh a sandbox's
packages after the fact, use `docker-sbx-update-sandboxes.py`, which runs on a sandbox that is
already up.

Extra volumes passed with `-v` are mounted **read-only by default**; append `:rw` to mount read-write (or `:ro` to be explicit). Each path is mounted inside the sandbox at the same absolute path it has on the host. Repeat `-v` for several mounts.

The project is mounted where `sbx` puts it: the same absolute path it has on the host (a Windows
`C:\...` path is visible inside the sandbox as `/c/...`), and that is the directory the agent
starts in and the default working directory of `sbx exec`. `docker-sbx` does **not** create a
`~/workspace` symlink to it — pointing one at the host spelling of the path left a dangling link
inside the sandbox.

### Examples

Project is in user's directory user's projects/test01 :
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01
```

Project's files are in /mnt/c/Users/johndoe/projects/test01 (WSL) :
```
python3 docker-sbx-create-sandbox.py -n test01 -p /mnt/c/Users/johndoe/projects/test01
```

Project's files are in user's projects/test01 and Anthropic API key is provided in private/.anthropic.api.env :
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -e $HOME/private/.anthropic.api.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server :
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server, using a custom secrets files settings :
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env -sf secrets.json
```

Generate a default secrets files settings json :
```
python3 docker-sbx-create-sandbox.py -gsf secrets.json
```

Use a Python profile to allow access to PyPI:
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -profile python
```

Use multiple profiles (order matters):
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -profile python,node
```

Use a custom profile directory:
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -profile myprofile -profile-directory $HOME/my-profiles
```

Mount an extra directory read-only and another one read-write:
```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -v /mnt/c/docs -v /mnt/c/data:rw
```

.anthropic.api.env :
```
ANTHROPIC_API_KEY=[your anthropic API key]
```

.anthropic.auth.env :
```
ANTHROPIC_AUTH_TOKEN=[your anthropic account token]
```

.ollama.srv.env :
```
ANTHROPIC_BASE_URL=http{s}://[ollama host]{:[ollama port]}
ANTHROPIC_AUTH_TOKEN=ollama
```

Example for locally dockerized ollama server with port 11434:
```
ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ANTHROPIC_AUTH_TOKEN=ollama
```


## Policy Profiles

By default every sandbox shares the same restrictive network policy. A **profile** adds
extra **per-sandbox** network rules, so a project that needs PyPI or npm can reach it
without opening that domain up for every sandbox.

Profiles are YAML files in the `./profiles` directory (or the directory given with
`-profile-directory`) and are selected with `-profile`. They are applied after all
standard rules, so profile rules take precedence.

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
- Any key that would imply a global policy is rejected; profiles are always per-sandbox.
- Malformed or unknown profiles abort sandbox creation.

### Standard profiles

| Profile | Grants access to |
|---|---|
| `python` | PyPI and test PyPI |
| `java` | Maven Central and Gradle repositories |
| `java-spring-boot` | Java domains plus Spring repositories and `start.spring.io` |
| `go` | Go module proxy and common VCS hosts (GitHub, GitLab, Bitbucket) |
| `node` | npm and Yarn registries |
| `rust` | crates.io |
| `dotnet` | NuGet.org |
| `flutter` | pub.dev and Google Storage for Flutter SDK / Dart packages |

Example:

```bash
python3 docker-sbx-create-sandbox.py -n pyproject -p "$HOME/projects/pyproject" -profile python
python3 docker-sbx-create-sandbox.py -n spring -p "$HOME/projects/spring" -profile java-spring-boot
python3 docker-sbx-create-sandbox.py -n fullstack -p "$HOME/projects/fullstack" -profile python,node
```


## Notifications (optional)

A sandbox can tell you what its Claude Code is doing without you having to attach to it.
Two providers are supported — **Slack** and **Nextcloud Talk** — and each is switched on
independently, so a sandbox can notify over Slack only, over Talk only, or over both at the
same time. When the env file passed with `-e` enables one, the sandbox gets Claude Code
hooks that post one short line per lifecycle event:

```
[test01] test01 — Claude Code is done
```

Both are off unless asked for: with no `SBX_SLACK_*` and no `SBX_NCTALK_*` variables in the
env file, nothing is written and the sandbox behaves exactly as before. The two providers
share the same event vocabulary (`Stop`, `Notification`, `SessionStart`, `SessionEnd`,
`SubagentStop`, `UserPromptSubmit`), the same default set
(`Stop,Notification,SessionStart,SessionEnd`), the same per-event override scheme, and the
same message format. Enabling one never affects the other.

## Slack notifications (optional)

Set `SBX_SLACK_ENABLED=true` to notify a Slack channel.

### Before you start: a Slack app and a dedicated channel

Nothing here posts to Slack as *you*. Both credential styles post as an **app**, so two
things must exist before any of it can work:

1. **A Slack app, declared in your workspace.** Create one at
   [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From
   scratch**, and pick the workspace you want the notifications in. `docker-sbx` never
   registers anything for you — it only uses the credential you put in the env file, and
   Slack is the side that authorises it.
2. **A destination the app is allowed to post to** — a dedicated channel that you either
   select when creating the webhook, or invite the app into (bot-token mode). An app
   belongs to no channel by default, not even a private channel you created yourself, and
   `chat.postMessage` refuses with `not_in_channel` until it has been invited; inviting
   the app in both modes keeps that unambiguous. Your own DM with yourself is not a
   usable destination either way; see [Getting the channel id](#getting-the-channel-id).

So create a channel dedicated to this — `#docker-sbx`, say — invite the app to it with
`/invite @your-app`, and pick that same channel when you create the webhook. Keeping it
separate matters for two reasons: the notifications do not land in a conversation you read
as yourself, and the credential you place inside the sandbox is scoped to a channel you are
willing to let an agent post into.

### Getting a credential

Both styles start from that same app, and both are created in Slack:

**Webhook — least privilege.** In the app: **Incoming Webhooks** → switch it on → **Add
New Webhook to Workspace** → choose the channel you just created. Slack shows a URL bound
to that channel. That URL *is* the credential, so it can only ever post to that one
channel.

**Bot token.** In the app: **OAuth & Permissions** → add the **`chat:write`** scope (add
**`im:write`** as well only if you also want the app to be able to DM you) → **Install to
Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`). Then look up the destination
channel's id — [Getting the channel id](#getting-the-channel-id) — and confirm the app has
been invited to it.

### Setup

Write an env file (for example `$HOME/private/.slack.env`) with **one** of the two
credentials:

```
SBX_SLACK_ENABLED=true
SBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00000000/B00000000/xxxxxxxx
```

```
SBX_SLACK_ENABLED=true
SBX_SLACK_BOT_TOKEN=xoxb-…
SBX_SLACK_CHANNEL=C0123456789
```

and pass it like any other env file:

```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -e $HOME/private/.slack.env
```

### Variables

| Variable | Default | Purpose |
|---|---|---|
| `SBX_SLACK_ENABLED` | `false` | Master switch. Anything other than `true` leaves the sandbox untouched. |
| `SBX_SLACK_WEBHOOK_URL` | – | Incoming webhook URL. Bound to a single channel by Slack. |
| `SBX_SLACK_BOT_TOKEN` | – | Bot token (`xoxb-…`, needs the `chat:write` scope). Requires `SBX_SLACK_CHANNEL`. |
| `SBX_SLACK_CHANNEL` | – | Default channel **id** for the bot-token mode — not a name. See [Getting the channel id](#getting-the-channel-id). |
| `SBX_SLACK_EVENTS` | `Stop,Notification,SessionStart,SessionEnd` | Which events to notify on. `SubagentStop` and `UserPromptSubmit` are also accepted. |
| `SBX_SLACK_PREFIX` | – | Extra text prepended to every message. |
| `SBX_SLACK_DRYRUN` | `false` | Print the resolved message to stderr instead of posting it. |

If both credential styles are configured, the bot token wins. Any webhook or channel
variable can be redirected for a single event by appending the upper-cased event name —
`SBX_SLACK_WEBHOOK_NOTIFICATION=…`, for instance, sends `Notification` messages somewhere
other than everything else.

Messages are deliberately short: sandbox name, project name, and a fixed phrase per event
(`Claude Code is done`, `Claude Code waits for your input`, `Claude Code session started`…).
For `Notification` events, Claude Code's own reason for interrupting is appended on a
second line.

### Getting the channel id

Bot-token mode needs a channel **id**, not a channel name. A name is accepted unreliably at
best, so `#my-channel` or `my-channel` commonly fails with `channel_not_found`; an id always
works, and keeps working when the channel is renamed.

To find it, open the channel in Slack, click its name, and choose **View channel
details**: the id is at the bottom of the panel. Alternatively **Copy link** and take the
trailing segment of the URL — in `…/archives/C0123456789`, the id is `C0123456789`.

```
C0123456789   public channel
G0123456789   private channel
D0123456789   direct message the app is a participant in
```

**A personal DM cannot be used.** The conversation in your sidebar under your own name —
the one where you message yourself — carries a `D…` id as well, but it is a dialogue
between you and yourself, so no app can be a participant in it and the bot token cannot
see it at all. Pointing `SBX_SLACK_CHANNEL` at it fails with `channel_not_found` even
though the id is exactly right. A `D…` id works only when it is a DM the app itself is
part of, which is what `conversations.open` returns for a user id (needs `im:write`); for
notifications, a public or private channel the app has been invited to is the simpler
target.

The bot must also be a member of that channel, or `chat.postMessage` still fails — with
`not_in_channel` this time, even when the id is correct. Invite it from inside the
channel with `/invite @your-app`.

Both failures, and the channel value that was actually sent, are recorded in
`~/.claude/hooks/slack-errors.log` inside the sandbox — check it there rather than
guessing, since `channel_not_found` and `not_in_channel` are indistinguishable from
Slack's reply alone.

If you would rather read the ids from Slack than from the UI, `conversations.list` works
from inside the sandbox (`slack.com` is already allowed), but it needs `channels:read` /
`groups:read` in addition to `chat:write`:

```bash
set -a; . ~/.claude/hooks/slack.env; set +a
curl -s -H "Authorization: Bearer $SBX_SLACK_BOT_TOKEN" \
  'https://slack.com/api/conversations.list?types=public_channel,private_channel&limit=200' \
  | jq -r '.channels[] | "\(.id)\t\(.name)"'
```

### How it is wired, and what it costs you

- A hook script is installed at `~/.claude/hooks/slack-notify.sh` inside the sandbox, and
  `hooks` entries for the chosen events are merged into `~/.claude/settings.json`. Existing
  hooks are preserved, and re-running setup replaces rather than duplicates its own entries.
- The credential lives in `~/.claude/hooks/slack.env`, mode `600`, outside the project
  volume. It is still readable by the agent running in that sandbox — so scope the
  credential accordingly. An incoming webhook bound to one channel is the least-privilege
  option; a bot token can post to every channel the bot belongs to.
- Network policy is extended **per sandbox** with only the endpoint that credential style
  needs: `hooks.slack.com` for webhooks, `slack.com` for a bot token. Nothing global changes.
- Bot tokens and secret webhook URLs are supplied to curl through its stdin configuration,
  keeping them out of the process argument list.
- Every hook exits `0` and never writes to stdout, so a Slack outage cannot block or
  derail the agent. A failed post therefore cannot interrupt you, but it is **not** silent:
  the reason is appended to `~/.claude/hooks/slack-errors.log` (mode `600`, one line per
  failure, created only when something fails). Check it first if a message never arrives —
  it distinguishes a webhook Slack rejected, a bot that was not invited to the channel
  (`not_in_channel`), a bad token (`invalid_auth`), and a request the sandbox's network
  policy dropped (HTTP `403`). The webhook path is redacted, since it is the credential.
  `SBX_SLACK_DRYRUN=true` still resolves and prints the message to stderr without posting.
- The env file is read as-is, so a CRLF file is fine: the sandbox strips the carriage
  return, exactly as the host side already did.


## Nextcloud Talk notifications (optional)

Set `SBX_NCTALK_ENABLED=true` to notify a Nextcloud Talk room instead of (or as well as)
Slack. It posts through the Talk **spreed** API using a Nextcloud user's app password, so
nothing is provisioned for you: the user, the credential and the room must already exist.

### Getting a credential

1. **A Nextcloud user** that is a member of the room you want the messages in. A dedicated
   account is least privilege: the app password you place in the sandbox can act as that
   user for every API call, so scope it to an account you are willing to expose there.
2. **An app password** for that user: in Nextcloud, **Settings → Security → Devices &
   sessions → Create new app password**. Copy the generated password — it is shown once.
   Your normal login password will not work with the API.
3. **The room token** of the Talk conversation. Open the room and read it from the URL:
   in `https://cloud.example.com/call/abc123XYZ`, the token is `abc123XYZ`. The room must
   be one the user is a member of.

### Setup

Write an env file (for example `$HOME/private/.nctalk.env`):

```
SBX_NCTALK_ENABLED=true
SBX_NCTALK_URL=https://cloud.example.com
SBX_NCTALK_USER=agent
SBX_NCTALK_TOKEN=xxxxx-xxxxx-xxxxx-xxxxx-xxxxx
SBX_NCTALK_ROOM=abc123XYZ
```

and pass it like any other env file:

```
python3 docker-sbx-create-sandbox.py -n test01 -p $HOME/projects/test01 -e $HOME/private/.nctalk.env
```

### Variables

| Variable | Default | Purpose |
|---|---|---|
| `SBX_NCTALK_ENABLED` | `false` | Master switch. Anything other than `true` leaves the sandbox untouched. |
| `SBX_NCTALK_URL` | – | Base URL of the Nextcloud instance (scheme + host; a trailing slash is fine). |
| `SBX_NCTALK_USER` | – | Nextcloud user id the app password belongs to. |
| `SBX_NCTALK_TOKEN` | – | App password for that user. |
| `SBX_NCTALK_ROOM` | – | Talk room token (see above). |
| `SBX_NCTALK_EVENTS` | `Stop,Notification,SessionStart,SessionEnd` | Which events to notify on. `SubagentStop` and `UserPromptSubmit` are also accepted. |
| `SBX_NCTALK_PREFIX` | – | Extra text prepended to every message. |
| `SBX_NCTALK_DRYRUN` | `false` | Print the resolved message to stderr instead of posting it. |

All four of `SBX_NCTALK_URL`, `SBX_NCTALK_USER`, `SBX_NCTALK_TOKEN` and `SBX_NCTALK_ROOM`
are required when the feature is enabled; creation aborts with the missing variable named
otherwise. The room can be redirected for a single event by appending the upper-cased event
name — `SBX_NCTALK_ROOM_NOTIFICATION=…`, for instance, sends `Notification` messages to a
different room.

The message format is the same as Slack's: `[<sandbox>] <project> — <phrase>`, with Claude
Code's own reason appended on a second line for `Notification` events. Unlike Slack, a
Nextcloud Talk message is Markdown with no link/mention entity, so the text is sent verbatim
rather than HTML-escaped.

### How it is wired, and what it costs you

- A hook script is installed at `~/.claude/hooks/nctalk-notify.sh` inside the sandbox, and
  `hooks` entries for the chosen events are merged into `~/.claude/settings.json` alongside
  any Slack entries. Existing hooks are preserved, and re-running setup replaces rather than
  duplicates its own entries, per provider.
- The credential lives in `~/.claude/hooks/nctalk.env`, mode `600`, outside the project
  volume and separate from `slack.env`. It is still readable by the agent running in that
  sandbox, so scope the app password accordingly.
- Network policy is extended **per sandbox** with the Nextcloud instance's host only.
  Nothing global changes.
- Every hook exits `0` and never writes to stdout, so an outage cannot block or derail the
  agent. A failed post is **not** silent: the reason — the OCS status, the room and the
  response body — is appended to `~/.claude/hooks/nctalk-errors.log` (mode `600`, one line
  per failure, created only when something fails). Check it first if a message never
  arrives. `SBX_NCTALK_DRYRUN=true` still resolves and prints the message to stderr without posting.
- The endpoint used is
  `<SBX_NCTALK_URL>/ocs/v2.php/apps/spreed/api/v1/chat/<room>`, with the app password sent
  as HTTP basic auth through curl's stdin configuration rather than its process arguments,
  and with the `OCS-APIRequest: true` header Nextcloud requires.


## Use a sandbox

Standard launch command is `sbx run [sandbox name]`, Claude should execute automatically within project's directory.

Run `sbx exec -ti [sandbox name] bash` if you need to take a look or fix something from within the sandbox.


## Update sandboxes

`docker-sbx-update-sandboxes.py` refreshes the OS packages inside existing sandboxes:
for each one it runs `sudo apt update`, then `sudo apt upgrade -y`, then
`sudo apt autoremove -y`. With no arguments it processes every sandbox returned by
`sbx ls`.

```
python3 docker-sbx-update-sandboxes.py                       # every sandbox
python3 docker-sbx-update-sandboxes.py -i bc,web-1           # only bc and web-1
python3 docker-sbx-update-sandboxes.py -x bc,web-1           # all except bc and web-1
python3 docker-sbx-update-sandboxes.py -i bc,web-1 -x bc     # bc is skipped (exclusions win)
```

`-i`/`--include` and `-x`/`--exclude` take a comma- or space-separated list and are
repeatable. When you pass `-i`, the sandbox list is never fetched — useful when listing
is slow or unreliable.

The recommended invocation wraps it in `script`, because `sbx` wants a real TTY:

```
script -q /dev/null python3 docker-sbx-update-sandboxes.py [OPTIONS]
```

Useful environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `DRY_RUN=1` | `0` | Print the command that would run for each sandbox; change nothing. |
| `CLOUD=1` | `0` | Target cloud sandboxes (adds the global `--cloud` option). |
| `SBX_TIMEOUT` | `120` | Seconds allowed per query call (`sbx ls`, `version`, `daemon status`). The script enforces it itself; a value that is not a positive number stops the run. |
| `SBX_EXEC_FLAGS` | auto | Force the `sbx exec` flags instead of auto-detecting (`-ti` on a TTY, else `-i`). |
| `SBX_LS_CMD` | – | Custom listing command; sandbox names on stdout. |
| `APT_CHAIN` | – | Override the in-sandbox command chain. |
| `APT` | `apt-get` | Package manager (`apt-get` or `apt`). |
| `SUDO` | `sudo` | Command prefix; use `sudo -n` to never prompt and fail fast instead. |

Each sandbox is handled in turn and the run finishes with a summary
(`ok=… failed=… skipped=…`). The script exits non-zero if any sandbox failed.
