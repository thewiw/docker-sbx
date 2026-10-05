# Docker SBX

Tools to install [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine + create and manage AI Agent sandboxes

Work in progress !!!
Use at your own risk !!!

Tested on :

  - WSL Ubuntu 24.04 LTS and Ubuntu 26.04 LTS

  - Ubuntu 26.04 LTS

All scripts must be in same directory, and must be run from that directory


## Install engine

This must be done only once.

First [create a docker account](https://app.docker.com/signup/) if you do not have one already or if you want one for this installation.

Run `./docker-sbx-install.sh` and answer the questions (at one point you will need your docker account).

At the end of installation, all network communication from the sandboxes is prohibited, except for system updates


## Create a sandbox

This must be done each time a new sandbox must be created.

Each sandbox is based on 3 main components :
  - Claude Code as AI Agent
  - a shared volume where the project is
  - a network security policy (a restrictive default shared by all sandboxes — only HTTP, HTTPS and SSH protocols are allowed — which per-sandbox profiles can extend)

Claude Code has got write access to the whole sandbox filesystem, including the shared volume, so DO NOT STORE CREDENTIALS in this project volume

For maximum security, the shared volume should only contain project files (sources, git, ...) WITHOUT any credentials/certificates/... files.

For the same reason, sources history (git) MUST NOT contain credentials/certificates/... either (or worst-case scenario if those data exist then they must be obsolete).

```
./docker-sbx-create-sandbox.sh -n [sandbox name] -p [absolute path] -e {path to env file} -s {true/false} -sf {path to secrets files settings} -gsf {path to secrets files settings} -v {path} -profile {comma-separated profile names} -profile-directory {path to profile directory} :
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

Extra volumes passed with `-v` are mounted **read-only by default**; append `:rw` to mount read-write (or `:ro` to be explicit). Each path is mounted inside the sandbox at the same absolute path it has on the host. Repeat `-v` for several mounts.

In the end, project's files should be available within `~/workspace` in the sandbox.

### Examples

Project is in user's directory user's projects/test01 :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01
```

Project's files are in /mnt/c/Users/johndoe/projects/test01 (WSL) :
```
./docker-sbx-create-sandbox.sh -n test01 -p /mnt/c/Users/johndoe/projects/test01
```

Project's files are in user's projects/test01 and Anthropic API key is provided in private/.anthropic.api.env :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.anthropic.api.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server, using a custom secrets files settings :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env -sf secrets.json
```

Generate a default secrets files settings json :
```
./docker-sbx-create-sandbox.sh -gsf secrets.json
```

Use a Python profile to allow access to PyPI:
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -profile python
```

Use multiple profiles (order matters):
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -profile python,node
```

Use a custom profile directory:
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -profile myprofile -profile-directory $HOME/my-profiles
```

Mount an extra directory read-only and another one read-write:
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -v /mnt/c/docs -v /mnt/c/data:rw
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
./docker-sbx-create-sandbox.sh -n pyproject -p "$HOME/projects/pyproject" -profile python
./docker-sbx-create-sandbox.sh -n spring -p "$HOME/projects/spring" -profile java-spring-boot
./docker-sbx-create-sandbox.sh -n fullstack -p "$HOME/projects/fullstack" -profile python,node
```


## Slack notifications (optional)

A sandbox can tell you what its Claude Code is doing without you having to attach to it.
When the env file passed with `-e` enables it, the sandbox gets Claude Code hooks that
post one short line per lifecycle event:

```
[test01] test01 — Claude Code is done
```

It is off unless asked for: with no `SBX_SLACK_*` variables in the env file, nothing is
written and the sandbox behaves exactly as before.

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
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.slack.env
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
| `SBX_SLACK_DRYRUN` | `false` | Print the resolved message instead of posting it. |

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
- Every hook exits `0` and never writes to stdout, so a Slack outage cannot block or
  derail the agent. A failed post therefore cannot interrupt you, but it is **not** silent:
  the reason is appended to `~/.claude/hooks/slack-errors.log` (mode `600`, one line per
  failure, created only when something fails). Check it first if a message never arrives —
  it distinguishes a webhook Slack rejected, a bot that was not invited to the channel
  (`not_in_channel`), a bad token (`invalid_auth`), and a request the sandbox's network
  policy dropped (HTTP `403`). The webhook path is redacted, since it is the credential.
  `SBX_SLACK_DRYRUN=true` still resolves and prints the message without posting.
- The env file is read as-is, so a CRLF file is fine: the sandbox strips the carriage
  return, exactly as the host side already did.


## Use a sandbox

Standard launch command is `sbx run [sandbox name]`, Claude should execute automatically within project's directory.

Run `sbx exec -ti [sandbox name] bash` if you need to take a look or fix something from within the sandbox.


## Update sandboxes

`docker-sbx-update-sandboxes.sh` refreshes the OS packages inside existing sandboxes:
for each one it runs `sudo apt update`, then `sudo apt upgrade -y`, then
`sudo apt autoremove -y`. With no arguments it processes every sandbox returned by
`sbx ls`.

```
./docker-sbx-update-sandboxes.sh                       # every sandbox
./docker-sbx-update-sandboxes.sh -i bc,web-1           # only bc and web-1
./docker-sbx-update-sandboxes.sh -x bc,web-1           # all except bc and web-1
./docker-sbx-update-sandboxes.sh -i bc,web-1 -x bc     # bc is skipped (exclusions win)
```

`-i`/`--include` and `-x`/`--exclude` take a comma- or space-separated list and are
repeatable. When you pass `-i`, the sandbox list is never fetched — useful when listing
is slow or unreliable.

The recommended invocation wraps it in `script`, because `sbx` wants a real TTY:

```
script -q /dev/null ./docker-sbx-update-sandboxes.sh [OPTIONS]
```

Useful environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `DRY_RUN=1` | `0` | Print the command that would run for each sandbox; change nothing. |
| `CLOUD=1` | `0` | Target cloud sandboxes (adds the global `--cloud` option). |
| `SBX_TIMEOUT` | `120` | Seconds allowed per query call (`sbx ls`, `version`, `daemon status`). |
| `SBX_EXEC_FLAGS` | auto | Force the `sbx exec` flags instead of auto-detecting (`-ti` on a TTY, else `-i`). |
| `SBX_LS_CMD` | – | Custom listing command; sandbox names on stdout. |
| `APT_CHAIN` | – | Override the in-sandbox command chain. |
| `APT` | `apt-get` | Package manager (`apt-get` or `apt`). |
| `SUDO` | `sudo` | Command prefix; use `sudo -n` to never prompt and fail fast instead. |

Each sandbox is handled in turn and the run finishes with a summary
(`ok=… failed=… skipped=…`). The script exits non-zero if any sandbox failed.
