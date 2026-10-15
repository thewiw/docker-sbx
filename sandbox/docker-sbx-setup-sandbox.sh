prjpath=$1
envfilepath=${2:-}

stop_apt() {
  sleep 5

  curpid=$$
  # Match only command lines that *start* with a known apt/dpkg executable.
  # The previous unanchored 'apt(-|/)?' also matched any process whose argv
  # merely contained the substring "apt" - including an ancestor shell carrying
  # the project path (e.g. "-p /mnt/c/Projects/aptitude"), so this could SIGKILL
  # the setup script's own parent chain mid-run.
  apt_re='^(/(usr/)?(s?bin)/)?(apt|apt-get|apt-key|apt-config|apt-mark|dpkg|dpkg-deb|dpkg-query|dpkg-split)([[:space:]]|$)'
  apt_re+='|^/(usr/)?bin/python3[0-9.]* /usr/share/unattended-upgrades/'

  mapfile -t aptpids < <(
      pgrep -f "$apt_re" | grep -v "^$curpid$"
  )

  if [ ${#aptpids[@]} -eq 0 ]; then
    echo "✅  No apt‑related processes found – nothing to kill."
    return 0
  fi

  SECONDS=0

  printf "🔍  Current process: $curpid\n"

  while [[ ${#aptpids[@]} -gt 0 && SECONDS -lt 60 ]]; do
    printf "🔍  Apt processes: %s\n" "${aptpids[*]}"

    # ---- Graceful stop -------------------------------------------------
    if [[ SECONDS -lt 30 ]]; then
      sudo kill -TERM "${aptpids[@]}" 2>/dev/null || true
    else
      sudo kill -KILL "${aptpids[@]}" 2>/dev/null || true
    fi
    sleep 2 # give them a chance to clean up

    mapfile -t aptpids < <(
      pgrep -f "$apt_re" | grep -v "^$curpid$"
    )
  done

  if [ ${#aptpids[@]} -gt 0 ]; then
    printf "⚠️  Some apt processes survived SIGTERM  %s - sending SIGKILL\n" "${aptpids[*]}"
    sudo kill -KILL "${aptpids[@]}" 2>/dev/null || true
    sleep 5
  else
    printf "✅  All apt processes terminated cleanly\n"
  fi
}

install_tools() {
  sudo apt update -y
  sudo apt upgrade -y
  sudo apt install jq -y
}

set_env_if_missing() {
    if [[ $# -ne 2 ]]; then
        printf 'Usage: %s VAR_NAME VALUE\n' "${FUNCNAME[0]}"
        return 1
    fi

    local envname=$1
    local envvalue=$2

    if [[ ! "$envname" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        printf 'Invalid variable name "%s"\n' "$envname"
        return 1
    fi

    if [[ -z "${!envname:-}" ]]; then
        export "$envname=$envvalue"
    fi

    return 0
}

load_env_file() {
    # Source the env file passed in from the host (copied into the sandbox by
    # docker-sbx-create-sandbox.sh) so its variables are available to
    # update_claude_code_settings regardless of whether `sbx exec --env-file`
    # propagated them. Only KEY=VALUE assignments are honoured; blank lines and
    # lines starting with '#' are skipped (matching docker --env-file semantics,
    # so an inline '#' stays part of the value).
    if [[ -z "$envfilepath" ]]; then
        return 0
    fi

    if [[ ! -f "$envfilepath" ]]; then
        printf 'Warning: env file "%s" not found, skipping\n' "$envfilepath"
        return 0
    fi

    printf 'Loading env file: %s\n' "$envfilepath"
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip a trailing CR. An env file written by a Windows editor on WSL is
        # CRLF, and read leaves the CR in the value, so "true" arrives as "true\r"
        # and every exact-match test below silently fails. The host side already
        # trims whitespace (env_file_get), so without this the host would report
        # the Slack feature as enabled while the sandbox installed nothing.
        line="${line%$'\r'}"
        [[ -z "$line" || "$line" == "#"* ]] && continue
        if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            export "$line"
        fi
    done < "$envfilepath"
}

cleanup_env_file() {
    # The env file may contain credentials; remove the in-sandbox copy once the
    # settings have been written. Claude Code reads these from settings.json.
    if [[ -n "$envfilepath" && -f "$envfilepath" ]]; then
        rm -f "$envfilepath"
    fi
}

# ----------------------------------------------------------------------
# Optional Slack notifications -----------------------------------------
# ----------------------------------------------------------------------

unquote() {
  # load_env_file exports values verbatim, so a quoted value in the -e file
  # arrives with its quotes still attached. Tolerate both forms.
  local v="$1"
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "$v"
}

slack_var() {
  # Value of a SBX_SLACK_* variable, unquoted, from the exported environment.
  unquote "${!1:-}"
}

shell_single_quote() {
  # Render a value as a single-quoted shell literal. slack.env is sourced by the
  # hook on every event, so a project path containing quotes, spaces, <, & or
  # $(...) must not be able to break out of the assignment.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_slack_env() {
  local hooks_dir="$1"
  local events="$2"
  local target="$hooks_dir/slack.env"
  local tmp name value event suffix event_list

  tmp="$(mktemp "${target}.tmp.XXXXXX")" || return 1

  {
    printf 'SBX_SLACK_SANDBOX=%s\n' "$(shell_single_quote "$(slack_var SBX_SLACK_SANDBOX)")"
    printf 'SBX_SLACK_PROJECT=%s\n' "$(shell_single_quote "$(slack_var SBX_SLACK_PROJECT)")"

    for name in SBX_SLACK_WEBHOOK_URL SBX_SLACK_BOT_TOKEN SBX_SLACK_CHANNEL SBX_SLACK_PREFIX SBX_SLACK_DRYRUN; do
      value="$(slack_var "$name")"
      if [[ -n "$value" ]]; then
        printf '%s=%s\n' "$name" "$(shell_single_quote "$value")"
      fi
    done

    # Per-event overrides, e.g. Notification -> SBX_SLACK_WEBHOOK_NOTIFICATION.
    IFS=',' read -ra event_list <<< "$events"
    for event in "${event_list[@]}"; do
      event="${event// /}"
      if [[ -z "$event" ]]; then
        continue
      fi
      suffix="${event^^}"
      for name in "SBX_SLACK_WEBHOOK_${suffix}" "SBX_SLACK_CHANNEL_${suffix}"; do
        value="$(slack_var "$name")"
        if [[ -n "$value" ]]; then
          printf '%s=%s\n' "$name" "$(shell_single_quote "$value")"
        fi
      done
    done
  } > "$tmp" || { rm -f "$tmp"; return 1; }

  chmod 600 "$tmp"
  mv -f "$tmp" "$target"
}

wire_slack_hooks() {
  local hooks_dir="$1"
  local events="$2"
  local target="$HOME/.claude/settings.json"
  local tmp events_json

  if [[ ! -f "$target" ]]; then
    printf '{}\n' > "$target"
  fi

  events_json="$(printf '%s' "$events" | tr ',' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' | jq -R . | jq -sc . || true)"
  if [[ -z "$events_json" ]]; then
    return 1
  fi

  tmp="$(mktemp "${target}.tmp.XXXXXX")" || return 1

  if jq --arg script "$hooks_dir/slack-notify.sh" --argjson events "$events_json" '
      .hooks //= {} |
      # Drop anything docker-sbx installed earlier, so re-running setup does not
      # accumulate duplicates and an event that is no longer requested does not
      # keep its old hook. Identified by the command path.
      reduce (.hooks | keys_unsorted[]) as $k (.;
        .hooks[$k] = [ .hooks[$k][] | select(([.hooks[]?.command] | index($script)) == null) ]
        | if (.hooks[$k] | length) == 0 then del(.hooks[$k]) else . end) |
      # One entry per requested event. "matcher" is deliberately absent: these
      # are lifecycle events, not tool events.
      reduce ($events[]) as $e (.;
        .hooks[$e] = ((.hooks[$e] // []) + [{hooks:[{type:"command",command:$script,timeout:10}]}]))
    ' "$target" > "$tmp"; then
    mv -f "$tmp" "$target"
  else
    rm -f "$tmp"
    printf 'Error: could not update %s for the Slack hook - file left unchanged\n' "$target" >&2
    return 1
  fi
}

install_slack_hook() {
  local enabled
  enabled="$(slack_var SBX_SLACK_ENABLED)"
  # The host side trims surrounding whitespace (env_file_get), so mirror that here:
  # otherwise a padded value would leave the host staging a hook that this script
  # then refuses to install, which is the mismatch the warning below reports.
  enabled="${enabled#"${enabled%%[![:space:]]*}"}"
  enabled="${enabled%"${enabled##*[![:space:]]}"}"

  if [[ "$enabled" != "true" ]]; then
    # A set-but-unrecognised value used to be a silent no-op, leaving the host
    # reporting success while the sandbox installed nothing at all.
    if [[ -n "$enabled" ]]; then
      echo "Warning: SBX_SLACK_ENABLED is '$enabled', expected 'true' - skipping Slack hook"
    fi
    return 0
  fi

  local webhook token channel events
  webhook="$(slack_var SBX_SLACK_WEBHOOK_URL)"
  token="$(slack_var SBX_SLACK_BOT_TOKEN)"
  channel="$(slack_var SBX_SLACK_CHANNEL)"

  if [[ -z "$webhook" && ( -z "$token" || -z "$channel" ) ]]; then
    echo "Warning: SBX_SLACK_ENABLED is set but no usable Slack target, skipping"
    return 0
  fi

  events="$(slack_var SBX_SLACK_EVENTS)"
  if [[ -z "$events" ]]; then
    events="Stop,Notification,SessionStart,SessionEnd"
  fi

  # Labels shown in the message. The sandbox name is not in the hook payload, so
  # it is resolved here and baked into slack.env.
  SBX_SLACK_SANDBOX="${SANDBOX_NAME:-$(hostname 2>/dev/null || echo sandbox)}"
  SBX_SLACK_PROJECT="$(basename "${prjpath:-project}")"

  local hooks_dir="$HOME/.claude/hooks"
  mkdir -p "$hooks_dir"

  # The hook script is staged next to this script by docker-sbx-create-sandbox.sh.
  local stage="$HOME/slack-notify.sh"
  if [[ ! -f "$stage" ]]; then
    echo "Warning: $stage not found, skipping Slack hook"
    return 0
  fi
  mv -f "$stage" "$hooks_dir/slack-notify.sh"
  chmod 755 "$hooks_dir/slack-notify.sh"

  if ! write_slack_env "$hooks_dir" "$events"; then
    echo "Warning: could not write $hooks_dir/slack.env, skipping Slack hook"
    return 0
  fi

  if ! wire_slack_hooks "$hooks_dir" "$events"; then
    return 0
  fi

  echo "Slack notifications enabled for: $events"
}

update_claude_code_settings() {
  targetjson=~/.claude/settings.json

  # Seed the file so jq always has valid input to read. Without this, a missing
  # settings.json makes jq fail, and the (empty) temp file then overwrites it.
  if [[ ! -f "$targetjson" ]]; then
    mkdir -p "$(dirname "$targetjson")"
    printf '{}\n' > "$targetjson"
  fi

  tmpjson="$(mktemp "${targetjson}.tmp.XXXXXX" )" || exit 1

  set_env_if_missing DISABLE_TELEMETRY "1"
  set_env_if_missing CLAUDE_CODE_ENABLE_TELEMETRY "0"
  set_env_if_missing CLAUDE_CODE_DISABLE_TELEMETRY "1"
  set_env_if_missing CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC "1"
  set_env_if_missing CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY "1"

  #set_env_if_missing ANTHROPIC_API_KEY ""

  if [[ ! -z "$ANTHROPIC_BASE_URL" ]]; then
    set_env_if_missing CLAUDE_CODE_ATTRIBUTION_HEADER "0"
    set_env_if_missing ANTHROPIC_AUTH_TOKEN "setup_auth_token"
  #else
  #  set_env_if_missing CLAUDE_CODE_ATTRIBUTION_HEADER "1"
  fi

  echo "Updating Claude Code settings :"
  echo "  DISABLE_TELEMETRY = "${DISABLE_TELEMETRY}
  echo "  CLAUDE_CODE_ENABLE_TELEMETRY = "${CLAUDE_CODE_ENABLE_TELEMETRY}
  echo "  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC}
  if [[ ! -z "$CLAUDE_CODE_ATTRIBUTION_HEADER" ]]; then
    echo "  CLAUDE_CODE_ATTRIBUTION_HEADER = "${CLAUDE_CODE_ATTRIBUTION_HEADER}
  fi
  if [[ ! -z "$ANTHROPIC_BASE_URL" ]]; then
    echo "  ANTHROPIC_BASE_URL = "${ANTHROPIC_BASE_URL}
  fi
  if [[ ! -z "$ANTHROPIC_AUTH_TOKEN" ]]; then
    echo "  ANTHROPIC_AUTH_TOKEN = ******"
  fi
  if [[ ! -z "$ANTHROPIC_API_KEY" ]]; then
    echo "  ANTHROPIC_API_KEY = ******"
  fi

  if jq -e '
    .env //= {} |
    .env["DISABLE_TELEMETRY"] = $ENV.DISABLE_TELEMETRY |
    .env["CLAUDE_CODE_ENABLE_TELEMETRY"] = $ENV.CLAUDE_CODE_ENABLE_TELEMETRY |
    .env["CLAUDE_CODE_DISABLE_TELEMETRY"] = $ENV.CLAUDE_CODE_DISABLE_TELEMETRY |
    .env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = $ENV.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC |
    .env["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] = $ENV.CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY |
    if ( $ENV.CLAUDE_CODE_ATTRIBUTION_HEADER != null and $ENV.CLAUDE_CODE_ATTRIBUTION_HEADER != "" ) then .env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = $ENV.CLAUDE_CODE_ATTRIBUTION_HEADER else . end |
    if ( $ENV.ANTHROPIC_AUTH_TOKEN != null and $ENV.ANTHROPIC_AUTH_TOKEN != "" ) then .env["ANTHROPIC_AUTH_TOKEN"] = $ENV.ANTHROPIC_AUTH_TOKEN else . end |
    if ( $ENV.ANTHROPIC_API_KEY != null and $ENV.ANTHROPIC_API_KEY != "" ) then .env["ANTHROPIC_API_KEY"] = $ENV.ANTHROPIC_API_KEY else . end |
    if ( $ENV.ANTHROPIC_BASE_URL != null and $ENV.ANTHROPIC_BASE_URL != "" ) then .env["ANTHROPIC_BASE_URL"] = $ENV.ANTHROPIC_BASE_URL else . end
  ' "$targetjson" > "$tmpjson"; then
    mv -f "$tmpjson" "$targetjson"
  else
    rm -f "$tmpjson"
    printf 'Error: could not update %s (is it valid JSON?) - file left unchanged\n' "$targetjson" >&2
    exit 1
  fi
}

setup_path() {
  if [[ -z "$prjpath" ]]; then
    return 0
  fi

  cd "$HOME"
  rm -Rf ./workspace
  ln -s "$prjpath" ./workspace
}

setup_env() {
  # Idempotent: re-running setup must not accumulate duplicate lines. Guarded by
  # a marker comment rather than by grepping for the values, so a LANG or
  # LC_ALL the user set themselves is never re-appended.
  local bashrc=~/.bashrc
  touch "$bashrc"
  if grep -qF '# docker-sbx setup env' "$bashrc"; then
    return 0
  fi
  cat >> "$bashrc" <<'EOF'
# docker-sbx setup env
export SBX_NO_TELEMETRY=1
export LANG="C.utf8"
export LC_ALL="C.utf8"
EOF
}

echo "Project path: $prjpath"

load_env_file
stop_apt
install_tools
update_claude_code_settings
install_slack_hook
cleanup_env_file
setup_path
setup_env
