prjpath=$1
envfilepath=${2:-}

stop_apt() {
  sleep 5

  curpid=$$
  mapfile -t aptpids < <(
      pgrep -f 'apt(-|/)?' | grep -v "^$curpid$"
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
      pgrep -f 'apt(-|/)?' | grep -v "^$curpid$"
    )
  done

  if [ ${#aptpids[@]} -gt 0 ]; then
    printf "⚠️  Some apt processes survived SIGTERM  %s - sending SIGKILL" "${aptPpids*]}"
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

update_claude_code_settings() {
  targetjson=~/.claude/settings.json
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

  jq -e '
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
  ' "$targetjson" > "$tmpjson"

  mv -f "$tmpjson" "$targetjson"
}

setup_path() {
  if [[ -z prjpath ]]; then
    return 0
  fi

  cd "$HOME"
  rm -Rf ./workspace
  ln -s "$prjpath" ./workspace
}

setup_env() {
  echo 'export SBX_NO_TELEMETRY=1' >> ~/.bashrc
  echo 'export LANG="C.utf8"' >> ~/.bashrc
  echo 'export LC_ALL="C.utf8"' >> ~/.bashrc
}

echo "Project path: $prjpath"

load_env_file
stop_apt
install_tools
update_claude_code_settings
cleanup_env_file
setup_path
setup_env
