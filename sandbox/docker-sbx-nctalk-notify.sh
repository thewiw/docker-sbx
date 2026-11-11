#!/bin/bash
#
# docker-sbx Nextcloud Talk notification hook.
#
# Installed inside the sandbox as ~/.claude/hooks/nctalk-notify.sh and invoked by
# Claude Code with the hook payload as JSON on stdin. Its target - the Nextcloud
# base URL, the user, the app password and the Talk room token - plus the sandbox
# and project labels, come from ~/.claude/hooks/nctalk.env (mode 600) which
# docker-sbx-setup-sandbox.sh writes at sandbox-creation time. The variable names
# all carry the SBX_NCTALK_ prefix; SBX_NCTALK_ENABLED is the master switch, and
# it is the Nextcloud Talk counterpart of the Slack hook's SBX_SLACK_ENABLED, so a
# sandbox can be configured to notify over Slack, over Talk, or over both.
#
# This script sits on the critical path of every enabled hook event, so it must
# never block or fail the agent: it always exits 0, prints nothing on stdout, and
# caps every network call with --max-time. A post that fails is therefore silent
# on the agent's side, so the reason is recorded in nctalk-errors.log next to
# nctalk.env instead of being thrown away.
#
# Unlike Slack, Nextcloud Talk renders the message as Markdown and has no
# link/mention entity that a bare "<" would open, so the text is sent verbatim
# rather than HTML-escaped.

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
env_file="$script_dir/nctalk.env"

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

# ----------------------------------------------------------------------
# Diagnostics ----------------------------------------------------------
# ----------------------------------------------------------------------

# Record why a post did not land, one line per failure. Never writes to stdout and
# never fails: the hook still has to exit 0 on the agent's critical path.
log_failure() {
  local log="$script_dir/nctalk-errors.log"
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

# Host only, never the path. The room token is part of the request path and names
# the destination, so it is logged separately as room='...' - like Slack's channel
# id - while this keeps any query string out of the log.
log_safe_url() {
  printf '%s' "$1" | sed -E 's|(https?://[^/]+).*|\1/...|'
}

# Quote a value for curl's config syntax. The app password is fed to curl on
# stdin rather than in its argument list, where another process could see it.
curl_config_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\n'/\\n}"
  printf '%s' "$value"
}

curl_request_config() {
  printf 'user = "%s"\n' "$(curl_config_escape "$1:$2")"
}

# One POST. Leaves the HTTP status in NCTALK_STATUS and the single-lined response
# body in NCTALK_BODY, which the caller inspects. The OCS endpoint answers success
# with a 2xx and reports OCS errors with a non-2xx status carrying a meta envelope,
# so the status is the primary signal. Never fails itself: a transport error
# becomes "000" so a dropped request is distinguishable from a refused one.
#
# The headers are set here rather than at each call site. Nextcloud Talk requires
# OCS-APIRequest: true, and getting Content-Type wrong is not a loud failure:
# curl's default for --data-binary is application/x-www-form-urlencoded, which the
# OCS endpoint refuses rather than parsing as JSON.
post_request() {
  local url="$1" body="$2" user="$3" token="$4"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/sbx-nctalk-resp.XXXXXX" 2>/dev/null)" || tmp=""
  if [[ -z "$tmp" ]]; then
    curl_request_config "$user" "$token" | curl --silent --max-time 5 --output /dev/null \
      --config - \
      -H 'OCS-APIRequest: true' \
      -H 'Content-Type: application/json; charset=utf-8' \
      -H 'Accept: application/json' \
      --data-binary "$body" "$url" >/dev/null 2>&1 || true
    NCTALK_STATUS="000"
    NCTALK_BODY="no temp file available for the response"
    return 0
  fi
  NCTALK_STATUS="$(
    curl_request_config "$user" "$token" | curl --silent --max-time 5 --output "$tmp" \
      --write-out '%{http_code}' --config - \
      -H 'OCS-APIRequest: true' \
      -H 'Content-Type: application/json; charset=utf-8' \
      -H 'Accept: application/json' \
      --data-binary "$body" "$url" 2>/dev/null
  )" || NCTALK_STATUS="000"
  NCTALK_BODY="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"
  NCTALK_STATUS="${NCTALK_STATUS:-000}"
  # The network policy's block body is multi-line; keep the log one line per event.
  NCTALK_BODY="${NCTALK_BODY//$'\n'/ }"
  NCTALK_BODY="${NCTALK_BODY//$'\r'/ }"
  return 0
}

NCTALK_STATUS=""
NCTALK_BODY=""
# Bound what is written to the log without truncating what is parsed above.
log_body() {
  printf '%s' "${NCTALK_BODY:0:300}"
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

  text="${SBX_NCTALK_PREFIX:+$SBX_NCTALK_PREFIX }[${SBX_NCTALK_SANDBOX:-sandbox}] ${SBX_NCTALK_PROJECT:-project} — $phrase"
  if [[ -n "${detail:-}" ]]; then
    text="$text"$'\n'"$detail"
  fi

  # Per-event override falling back to the shared room. The variable suffix is the
  # upper-cased event name, e.g. Notification -> SBX_NCTALK_ROOM_NOTIFICATION.
  local suffix="${event^^}"
  local room_var="SBX_NCTALK_ROOM_${suffix}"
  local room="${!room_var:-${SBX_NCTALK_ROOM:-}}"

  local url="${SBX_NCTALK_URL:-}"
  local user="${SBX_NCTALK_USER:-}"
  local token="${SBX_NCTALK_TOKEN:-}"

  # Nothing to post with: the setup script already warned loudly when a target was
  # configured but incomplete, so stay silent on the event path.
  if [[ -z "$url" || -z "$user" || -z "$token" || -z "$room" ]]; then
    return 0
  fi

  # A trailing slash on the base URL would double up before /ocs/.
  url="${url%/}"
  local endpoint="$url/ocs/v2.php/apps/spreed/api/v1/chat/$room"

  if [[ "${SBX_NCTALK_DRYRUN:-false}" == "true" ]]; then
    # Resolve everything and report it on stderr, preserving the no-stdout hook
    # contract, but do not post or spam a real room.
    printf '%s\tevent=%s\troom=%s\ttext=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$event" "$room" "$text" >&2
    return 0
  fi

  local body
  body="$(jq -nc --arg m "$text" '{message:$m}' 2>/dev/null)" || {
    log_failure "event=$event could not build the body (is jq installed?)"
    return 0
  }

  # Basic auth is supplied through curl's stdin configuration, keeping the app
  # password out of the process argument list.
  post_request "$endpoint" "$body" "$user" "$token"

  if [[ "$NCTALK_STATUS" != 2* ]]; then
    # The room is logged with the failure because a wrong room token, a room the
    # user is not in, and a request blocked by the network policy are only
    # distinguishable by seeing exactly what was sent and what came back.
    log_failure "event=$event POST to $(log_safe_url "$endpoint") returned HTTP $NCTALK_STATUS for room='$room' - $(log_body)"
  fi

  return 0
}

notify || true
exit 0
