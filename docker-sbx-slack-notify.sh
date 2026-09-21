#!/bin/bash
#
# docker-sbx Slack notification hook.
#
# Installed inside the sandbox as ~/.claude/hooks/slack-notify.sh and invoked by
# Claude Code with the hook payload as JSON on stdin. Its target, and the sandbox
# and project labels, come from ~/.claude/hooks/slack.env (mode 600) which
# docker-sbx-setup-sandbox.sh writes at sandbox-creation time.
#
# This script sits on the critical path of every enabled hook event, so it must
# never block or fail the agent: it always exits 0, prints nothing on stdout, and
# caps every network call with --max-time. A post that fails is therefore silent
# on the agent's side, which used to make every cause - a revoked webhook, a bot
# that was never invited to the channel, a request dropped by the network policy -
# look exactly like success. The reason is now recorded in slack-errors.log next
# to slack.env instead of being thrown away.

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
env_file="$script_dir/slack.env"

# ----------------------------------------------------------------------
# One short phrase per event -------------------------------------------
# ----------------------------------------------------------------------
event_phrase() {
  case "$1" in
    SessionStart)     printf 'Claude Code session started' ;;
    SessionEnd)       printf 'Claude Code session ended' ;;
    Stop)             printf 'Claude Code is done' ;;
    Notification)     printf 'Claude Code waits for your input' ;;
    SubagentStop)     printf 'Subagent finished' ;;
    UserPromptSubmit) printf 'Prompt submitted' ;;
    *)                printf 'Claude Code event: %s' "${1:-unknown}" ;;
  esac
}

# Slack does not render HTML, and a bare "<" opens a link/mention entity, which
# can corrupt or reject the message. Escape the three significant characters.
# sed rather than ${var//pat/repl}: from bash 5.2 (Ubuntu 24.04+) patsub_replacement
# is on by default, so "&" in the replacement means "the matched text" and would
# turn "&lt;" into "<lt;".
escape_slack() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# ----------------------------------------------------------------------
# Diagnostics ----------------------------------------------------------
# ----------------------------------------------------------------------

# Record why a post did not land, one line per failure. Never writes to stdout and
# never fails: the hook still has to exit 0 on the agent's critical path.
log_failure() {
  local log="$script_dir/slack-errors.log"
  # The hook fires on every enabled event, so keep the file from growing without
  # bound. Dropping old lines is fine; the most recent reason is what matters.
  if [[ -f "$log" ]] && [[ "$(wc -c < "$log" 2>/dev/null || printf 0)" -gt 65536 ]]; then
    : > "$log" 2>/dev/null || return 0
  fi
  (
    umask 077
    printf '%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >> "$log"
  ) 2>/dev/null || true
  return 0
}

# Host only, never the path: a webhook URL carries its own secret in the path, so
# logging it verbatim would copy the credential into the log.
log_safe_url() {
  printf '%s' "$1" | sed -E 's|(https?://[^/]+).*|\1/...|'
}

# One POST. Leaves the HTTP status in SLACK_STATUS and the single-lined response
# body in SLACK_BODY, which the caller inspects: Slack reports API errors such as
# not_in_channel or invalid_auth as HTTP 200, so the body is authoritative and the
# status alone would not tell success from failure. Never fails itself: a transport
# error becomes "000" so a dropped request is distinguishable from a refused one.
#
# The Content-Type is set here rather than at each call site because getting it
# wrong is not a loud failure: curl's default for --data-binary is
# application/x-www-form-urlencoded, and Slack, told to expect form data but handed
# a JSON body, answers HTTP 200 {"ok":false,"error":"invalid_form_data"}.
post_request() {
  local url="$1" body="$2"
  shift 2
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/sbx-slack-resp.XXXXXX" 2>/dev/null)" || tmp=""
  if [[ -z "$tmp" ]]; then
    curl --silent --max-time 5 --output /dev/null \
      -H 'Content-type: application/json; charset=utf-8' \
      "$@" --data-binary "$body" "$url" >/dev/null 2>&1 || true
    SLACK_STATUS="000"
    SLACK_BODY="no temp file available for the response"
    return 0
  fi
  SLACK_STATUS="$(curl --silent --max-time 5 --output "$tmp" --write-out '%{http_code}' \
                    -H 'Content-type: application/json; charset=utf-8' \
                    "$@" --data-binary "$body" "$url" 2>/dev/null)" || SLACK_STATUS="000"
  SLACK_BODY="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"
  SLACK_STATUS="${SLACK_STATUS:-000}"
  # The network policy's block body is multi-line; keep the log one line per event.
  SLACK_BODY="${SLACK_BODY//$'\n'/ }"
  SLACK_BODY="${SLACK_BODY//$'\r'/ }"
  return 0
}

SLACK_STATUS=""
SLACK_BODY=""
# Bound what is written to the log without truncating what is parsed above: a
# successful chat.postMessage response is well over 300 bytes of JSON.
log_body() {
  printf '%s' "${SLACK_BODY:0:300}"
}

# ----------------------------------------------------------------------
# Main -----------------------------------------------------------------
# ----------------------------------------------------------------------
notify() {
  # Drain stdin first and completely, or Claude Code reports a failed write to
  # the hook process.
  local payload
  payload="$(cat)"

  [[ -f "$env_file" ]] || return 0
  # shellcheck source=/dev/null
  source "$env_file"

  local event
  event="$(printf '%s' "$payload" | jq -r '.hook_event_name // empty' 2>/dev/null || true)"

  local phrase detail text
  phrase="$(event_phrase "$event")"

  # Notification carries Claude Code's own reason for interrupting, which is far
  # more useful than our generic phrase, so append it when present.
  if [[ "$event" == "Notification" ]]; then
    local msg title
    msg="$(printf '%s' "$payload" | jq -r '.message // empty' 2>/dev/null || true)"
    title="$(printf '%s' "$payload" | jq -r '.title // empty' 2>/dev/null || true)"
    if [[ -n "$msg" ]]; then
      detail="$msg"
      if [[ -n "$title" ]]; then
        detail="$title: $msg"
      fi
    fi
  fi

  text="${SBX_SLACK_PREFIX:+$SBX_SLACK_PREFIX }[${SBX_SLACK_SANDBOX:-sandbox}] ${SBX_SLACK_PROJECT:-project} — $phrase"
  if [[ -n "${detail:-}" ]]; then
    text="$text"$'\n'"$detail"
  fi
  text="$(escape_slack "$text")"

  # Per-event override falling back to the shared target. The variable suffix is
  # the upper-cased event name, e.g. Notification -> SBX_SLACK_WEBHOOK_NOTIFICATION.
  local suffix="${event^^}"
  local url_var="SBX_SLACK_WEBHOOK_${suffix}"
  local chan_var="SBX_SLACK_CHANNEL_${suffix}"
  local url="${!url_var:-${SBX_SLACK_WEBHOOK_URL:-}}"
  local channel="${!chan_var:-${SBX_SLACK_CHANNEL:-}}"
  local token="${SBX_SLACK_BOT_TOKEN:-}"

  if [[ "${SBX_SLACK_DRYRUN:-false}" == "true" ]]; then
    # Resolve everything and log it, but do not post: lets the configuration be
    # checked without spamming a real channel.
    if [[ -n "$token" ]]; then
      printf '%s\tevent=%s\tchannel=%s\ttext=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$event" "$channel" "$text"
    else
      printf '%s\tevent=%s\turl=%s\ttext=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$event" "$url" "$text"
    fi
    return 0
  fi

  local body
  # Prefer the bot token when both forms are configured.
  if [[ -n "$token" && -n "$channel" ]]; then
    # The token travels as a header, so it is briefly visible in `ps` to the same
    # user that can already read slack.env; nothing new is exposed by this.
    body="$(jq -nc --arg c "$channel" --arg t "$text" '{channel:$c,text:$t}' 2>/dev/null)" || {
      log_failure "event=$event bot token mode: could not build the body (is jq installed?)"
      return 0
    }
    post_request https://slack.com/api/chat.postMessage "$body" -H "Authorization: Bearer $token"
    if [[ "$SLACK_STATUS" != 2* ]]; then
      log_failure "event=$event bot token mode: chat.postMessage returned HTTP $SLACK_STATUS for channel='$channel' - $(log_body)"
    elif [[ "$(printf '%s' "$SLACK_BODY" | jq -r '.ok // false' 2>/dev/null)" != "true" ]]; then
      # Slack answers API errors with HTTP 200, so the body is the only signal:
      # not_in_channel (the bot was never invited), channel_not_found (the channel
      # name is not an id), missing_scope, invalid_auth... The channel is logged
      # with the failure because those first two are only diagnosable by seeing
      # exactly what was sent: a name, a #name, or a value still carrying CRLF
      # all look alike from Slack's side.
      local slack_error hint
      slack_error="$(printf '%s' "$SLACK_BODY" | jq -r '.error // "unrecognised response"' 2>/dev/null || log_body)"
      # A D... id is a direct message, which exists only between two members. A
      # member's own DM with themselves is a D... id that is perfectly valid in
      # the Slack UI and still unreachable by any app, so it reports the same
      # channel_not_found as a typo'd id would. Say which of the two this is.
      hint=""
      if [[ "$channel" == D* && "$slack_error" == "channel_not_found" ]]; then
        hint=" (a D... id is a direct message, so the app must already be a participant in it - a personal DM with yourself cannot be posted to; open one with conversations.open, or invite the app to a real channel)"
      fi
      log_failure "event=$event bot token mode: chat.postMessage refused - $slack_error for channel='$channel'$hint"
    fi
    return 0
  fi

  if [[ -n "$url" ]]; then
    body="$(jq -nc --arg t "$text" '{text:$t}' 2>/dev/null)" || {
      log_failure "event=$event webhook mode: could not build the body (is jq installed?)"
      return 0
    }
    post_request "$url" "$body"
    # An incoming webhook answers 2xx with "ok"; anything else is a real failure.
    if [[ "$SLACK_STATUS" != 2* ]]; then
      log_failure "event=$event webhook mode: POST to $(log_safe_url "$url") returned HTTP $SLACK_STATUS - $(log_body)"
    fi
  fi

  return 0
}

notify || true
exit 0
