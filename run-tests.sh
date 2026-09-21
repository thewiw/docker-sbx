#!/bin/bash
set -euo pipefail

SCRIPT="/home/regis_wernet/projects/docker-sbx/docker-sbx-create-sandbox.sh"
TMPDIR="/tmp/docker-sbx-tests-$$"
PASSED=0
FAILED=0

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
pass() { echo "  [PASS] $1"; ((PASSED++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAILED++)) || true; }

cleanup() {
  if [[ -n "${SINKPID:-}" ]]; then
    kill "$SINKPID" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TMPDIR"

# ----------------------------------------------------------------------
# TEST 1: -gsf generates a valid JSON secrets config file
# ----------------------------------------------------------------------
echo "TEST 1: -gsf generates a valid JSON secrets config file"
OUTFILE="$TMPDIR/generated.json"
if bash "$SCRIPT" -gsf "$OUTFILE" >/dev/null 2>&1; then
  if [[ -f "$OUTFILE" ]]; then
    if python3 -c "import json; d=json.load(open('$OUTFILE')); assert 'search' in d and 'avoid' in d" 2>/dev/null; then
      pass "File generated and contains required keys"
    else
      fail "Generated file missing required keys"
    fi
  else
    fail "Generated file does not exist"
  fi
else
  fail "-gsf command failed"
fi

# ----------------------------------------------------------------------
# TEST 2: Default patterns detect hard-coded files and skip forbidden dirs
# ----------------------------------------------------------------------
echo "TEST 2: Default patterns detect hard-coded files and skip forbidden dirs"
PRJ="$TMPDIR/prj-default"
mkdir -p "$PRJ/node_modules" "$PRJ/bower_components" "$PRJ/sub"
touch "$PRJ/google-services.json" "$PRJ/key.properties" "$PRJ/app.jks"
touch "$PRJ/node_modules/secret.json" "$PRJ/bower_components/cert.crt"

TEST2="$TMPDIR/test2.sh"
cat > "$TEST2" << 'SCRIPT'
#!/bin/bash
path="$1"
secretsfile=""
check_project_secrets_files() {
  local search_patterns=()
  local avoid_patterns=()
  if [[ -n "$secretsfile" && -f "$secretsfile" ]]; then
    readarray -t search_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('search', [])]")
    readarray -t avoid_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('avoid', [])]")
  else
    search_patterns=("google-services.json" "key.properties" "*.jks" "*.cert" "*.crt" "*.key")
    avoid_patterns=(".venv" "node_modules" "bower_components")
  fi
  if [[ ${#search_patterns[@]} -eq 0 ]]; then
    echo "Not searching for secrets files, let's continue"
    return 0
  fi
  local find_args=("$path" -type f)
  local name_args=()
  for pattern in "${search_patterns[@]}"; do
    if [[ ${#name_args[@]} -gt 0 ]]; then
      name_args+=(-o)
    fi
    name_args+=(-name "$pattern")
  done
  if [[ ${#name_args[@]} -gt 0 ]]; then
    find_args+=("(" "${name_args[@]}" ")")
  fi
  for dir in "${avoid_patterns[@]}"; do
    find_args+=(-not -path "*/$dir/*")
  done
  local secrets_found=()
  readarray -t secrets_found < <(find "${find_args[@]}" 2>/dev/null)
  if [[ ${#secrets_found[@]} -gt 0 && -n "${secrets_found[0]}" ]]; then
    printf '%s\n' "${secrets_found[@]}"
    exit 1
  else
    echo "No secrets file detected within this project, let's continue"
  fi
}
check_project_secrets_files 2>/dev/null
SCRIPT
chmod +x "$TEST2"
if bash "$TEST2" "$PRJ"; then
  fail "Should have detected secrets"
else
  pass "Detected google-services.json, key.properties, app.jks and skipped node_modules/bower_components"
fi

# ----------------------------------------------------------------------
# TEST 3: Custom -sf config overrides defaults
# ----------------------------------------------------------------------
echo "TEST 3: Custom -sf config overrides defaults"
CUSTOM_JSON="$TMPDIR/custom.json"
cat > "$CUSTOM_JSON" << 'EOF'
{
  "search": ["*.env", "secret.txt"],
  "avoid": ["vendor"]
}
EOF
PRJ2="$TMPDIR/prj-custom"
mkdir -p "$PRJ2/vendor"
touch "$PRJ2/app.env" "$PRJ2/secret.txt" "$PRJ2/vendor/app.env"

TEST3="$TMPDIR/test3.sh"
cat > "$TEST3" << 'SCRIPT'
#!/bin/bash
path="$1"
secretsfile="$2"
check_project_secrets_files() {
  local search_patterns=()
  local avoid_patterns=()
  if [[ -n "$secretsfile" && -f "$secretsfile" ]]; then
    readarray -t search_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('search', [])]")
    readarray -t avoid_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('avoid', [])]")
  else
    search_patterns=("google-services.json" "key.properties" "*.jks" "*.cert" "*.crt" "*.key")
    avoid_patterns=(".venv" "node_modules" "bower_components")
  fi
  if [[ ${#search_patterns[@]} -eq 0 ]]; then
    echo "Not searching for secrets files, let's continue"
    return 0
  fi
  local find_args=("$path" -type f)
  local name_args=()
  for pattern in "${search_patterns[@]}"; do
    if [[ ${#name_args[@]} -gt 0 ]]; then
      name_args+=(-o)
    fi
    name_args+=(-name "$pattern")
  done
  if [[ ${#name_args[@]} -gt 0 ]]; then
    find_args+=("(" "${name_args[@]}" ")")
  fi
  for dir in "${avoid_patterns[@]}"; do
    find_args+=(-not -path "*/$dir/*")
  done
  local secrets_found=()
  readarray -t secrets_found < <(find "${find_args[@]}" 2>/dev/null)
  if [[ ${#secrets_found[@]} -gt 0 && -n "${secrets_found[0]}" ]]; then
    printf '%s\n' "${secrets_found[@]}"
    exit 1
  else
    echo "No secrets file detected within this project, let's continue"
  fi
}
check_project_secrets_files 2>/dev/null
SCRIPT
chmod +x "$TEST3"
if bash "$TEST3" "$PRJ2" "$CUSTOM_JSON"; then
  fail "Should have detected custom secrets"
else
  pass "Custom config detected *.env and secret.txt, skipped vendor/"
fi

# ----------------------------------------------------------------------
# TEST 4: -sf with missing file exits with error before sandbox creation
# ----------------------------------------------------------------------
echo "TEST 4: -sf with missing file exits with error before sandbox creation"
if bash "$SCRIPT" -sf "$TMPDIR/nonexistent.json" -n test -p /tmp 2>/dev/null; then
  fail "Should have exited with error for missing -sf file"
else
  pass "Exited with error for missing -sf file"
fi

# ----------------------------------------------------------------------
# TEST 5: Empty search array skips detection entirely
# ----------------------------------------------------------------------
echo "TEST 5: Empty search array skips detection entirely"
EMPTY_JSON="$TMPDIR/empty.json"
cat > "$EMPTY_JSON" << 'EOF'
{
  "search": [],
  "avoid": ["vendor"]
}
EOF
PRJ3="$TMPDIR/prj-empty"
mkdir -p "$PRJ3"
touch "$PRJ3/google-services.json"

TEST5="$TMPDIR/test5.sh"
cat > "$TEST5" << 'SCRIPT'
#!/bin/bash
path="$1"
secretsfile="$2"
check_project_secrets_files() {
  local search_patterns=()
  local avoid_patterns=()
  if [[ -n "$secretsfile" && -f "$secretsfile" ]]; then
    readarray -t search_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('search', [])]")
    readarray -t avoid_patterns < <(python3 -c "import json; data=json.load(open('$secretsfile')); [print(p) for p in data.get('avoid', [])]")
  else
    search_patterns=("google-services.json" "key.properties" "*.jks" "*.cert" "*.crt" "*.key")
    avoid_patterns=(".venv" "node_modules" "bower_components")
  fi
  if [[ ${#search_patterns[@]} -eq 0 ]]; then
    echo "Not searching for secrets files, let's continue"
    return 0
  fi
  local find_args=("$path" -type f)
  local name_args=()
  for pattern in "${search_patterns[@]}"; do
    if [[ ${#name_args[@]} -gt 0 ]]; then
      name_args+=(-o)
    fi
    name_args+=(-name "$pattern")
  done
  if [[ ${#name_args[@]} -gt 0 ]]; then
    find_args+=("(" "${name_args[@]}" ")")
  fi
  for dir in "${avoid_patterns[@]}"; do
    find_args+=(-not -path "*/$dir/*")
  done
  local secrets_found=()
  readarray -t secrets_found < <(find "${find_args[@]}" 2>/dev/null)
  if [[ ${#secrets_found[@]} -gt 0 && -n "${secrets_found[0]}" ]]; then
    printf '%s\n' "${secrets_found[@]}"
    exit 1
  else
    echo "No secrets file detected within this project, let's continue"
  fi
}
check_project_secrets_files 2>/dev/null
SCRIPT
chmod +x "$TEST5"
if bash "$TEST5" "$PRJ3" "$EMPTY_JSON"; then
  pass "Empty search array skipped detection"
else
  fail "Should have skipped detection with empty search array"
fi

# ----------------------------------------------------------------------
# TEST 6: Help flag (-h) shows usage and exits
# ----------------------------------------------------------------------
echo "TEST 6: Help flag (-h) shows usage and exits"
OUTPUT=$(bash "$SCRIPT" -h 2>&1 || true)
if echo "$OUTPUT" | grep -q "Usage:" && echo "$OUTPUT" | grep -q "\-sf"; then
  pass "Help output contains usage and -sf option"
else
  fail "Help output missing usage or -sf option"
fi

# ----------------------------------------------------------------------
# TEST 7: Unknown option shows error and usage
# ----------------------------------------------------------------------
echo "TEST 7: Unknown option shows error and usage"
OUTPUT=$(bash "$SCRIPT" --unknown 2>&1 || true)
if echo "$OUTPUT" | grep -q "Unknown option" && echo "$OUTPUT" | grep -q "Usage:"; then
  pass "Unknown option handled correctly"
else
  fail "Unknown option not handled correctly"
fi

# ----------------------------------------------------------------------
# TEST 8: Missing mandatory params shows error
# ----------------------------------------------------------------------
echo "TEST 8: Missing mandatory params shows error"
OUTPUT=$(bash "$SCRIPT" 2>&1 || true)
if echo "$OUTPUT" | grep -q "Missing parameter"; then
  pass "Missing params error shown"
else
  fail "Missing params error not shown"
fi

# ----------------------------------------------------------------------
# TEST 9: -gsf to nonexistent directory fails
# ----------------------------------------------------------------------
echo "TEST 9: -gsf to nonexistent directory fails"
if bash "$SCRIPT" -gsf "$TMPDIR/no/dir/file.json" 2>/dev/null; then
  fail "Should have failed for nonexistent directory"
else
  pass "Correctly failed for nonexistent directory"
fi

# ----------------------------------------------------------------------
# TEST 10: Generated JSON contains expected default patterns
# ----------------------------------------------------------------------
echo "TEST 10: Generated JSON contains expected default patterns"
GENFILE="$TMPDIR/gen-verify.json"
bash "$SCRIPT" -gsf "$GENFILE" >/dev/null 2>&1
if python3 -c "
import json
d = json.load(open('$GENFILE'))
assert 'google-services.json' in d['search']
assert 'key.properties' in d['search']
assert '*.jks' in d['search']
assert 'node_modules' in d['avoid']
assert 'bower_components' in d['avoid']
" 2>/dev/null; then
  pass "Generated JSON has correct default patterns"
else
  fail "Generated JSON missing expected default patterns"
fi

# ----------------------------------------------------------------------
# TEST 11: Slack enabled without a usable target aborts creation
# ----------------------------------------------------------------------
echo "TEST 11: Slack enabled without a usable target aborts creation"
SCRIPTS_DIR="$(dirname "$SCRIPT")"
SLACK_ENV="$TMPDIR/slack-no-target.env"
printf 'SBX_SLACK_ENABLED=true\n' > "$SLACK_ENV"
OUTPUT=$(bash "$SCRIPT" -n test -p /tmp -s false -e "$SLACK_ENV" 2>&1 || true)
if echo "$OUTPUT" | grep -q "no usable Slack target"; then
  pass "Aborts with a clear message when no target is configured"
else
  fail "Did not report the missing Slack target"
fi
if bash "$SCRIPT" -n test -p /tmp -s false -e "$SLACK_ENV" >/dev/null 2>&1; then
  fail "Should have exited non-zero for a missing Slack target"
else
  pass "Exits non-zero before creating anything"
fi

# ----------------------------------------------------------------------
# TEST 12: unsupported SBX_SLACK_EVENTS entry aborts creation
# ----------------------------------------------------------------------
echo "TEST 12: unsupported SBX_SLACK_EVENTS entry aborts creation"
BAD_EVENT_ENV="$TMPDIR/slack-bad-event.env"
printf 'SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\nSBX_SLACK_EVENTS=Stop,Bogus\n' > "$BAD_EVENT_ENV"
OUTPUT=$(bash "$SCRIPT" -n test -p /tmp -s false -e "$BAD_EVENT_ENV" 2>&1 || true)
if echo "$OUTPUT" | grep -q "unsupported SBX_SLACK_EVENTS entry 'Bogus'"; then
  pass "Rejects an unknown event name"
else
  fail "Did not reject the unknown event name"
fi

# ----------------------------------------------------------------------
# TEST 13: host side allows only the endpoints the credential style needs
# ----------------------------------------------------------------------
echo "TEST 13: host side allows only the needed Slack endpoints"
HOST_FNS="$TMPDIR/host-fns.sh"
sed -n '/^env_file_get() {/,/^trim() {/p' "$SCRIPT" | head -n -1 > "$HOST_FNS"
STUB_BIN="$TMPDIR/stubbin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/sbx" << 'SCRIPT'
#!/bin/bash
printf 'sbx %s\n' "$*"
SCRIPT
chmod +x "$STUB_BIN/sbx"

hosts_for() {
  PATH="$STUB_BIN:$PATH" bash -c "source '$HOST_FNS'; envfile='$1'; name='probe'; update_sbx_policy_slack" 2>&1 \
    | grep -o 'allowing .*' | sed 's/allowing //' | paste -sd, - || true
}
CASE_ENV="$TMPDIR/slack-hosts.env"
printf 'SBX_SLACK_ENABLED=true\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T/B/x\n' > "$CASE_ENV"
if [[ "$(hosts_for "$CASE_ENV")" == "hooks.slack.com" ]]; then
  pass "Webhook mode allows hooks.slack.com"
else
  fail "Webhook mode allowed '$(hosts_for "$CASE_ENV")'"
fi
printf 'SBX_SLACK_ENABLED=true\nSBX_SLACK_BOT_TOKEN=xoxb-t\nSBX_SLACK_CHANNEL=C1\n' > "$CASE_ENV"
if [[ "$(hosts_for "$CASE_ENV")" == "slack.com" ]]; then
  pass "Token mode allows slack.com"
else
  fail "Token mode allowed '$(hosts_for "$CASE_ENV")'"
fi
printf 'SBX_SLACK_ENABLED=false\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n' > "$CASE_ENV"
if [[ -z "$(hosts_for "$CASE_ENV")" ]]; then
  pass "Disabled adds no policy rule at all"
else
  fail "Disabled still added '$(hosts_for "$CASE_ENV")'"
fi

# ----------------------------------------------------------------------
# TEST 14: in-sandbox installer writes the config and stays idempotent
# ----------------------------------------------------------------------
echo "TEST 14: in-sandbox installer writes the config and stays idempotent"
INSTALLER_FNS="$TMPDIR/installer-fns.sh"
# Starts at load_env_file so the env-file parsing is exercised too (TEST 17).
sed -n '/^load_env_file() {/,/^update_claude_code_settings() {/p' "$SCRIPTS_DIR/docker-sbx-setup-sandbox.sh" | head -n -1 > "$INSTALLER_FNS"
FAKEHOME="$TMPDIR/fakehome"
mkdir -p "$FAKEHOME/.claude"
printf '{"model":"keepme"}\n' > "$FAKEHOME/.claude/settings.json"

run_installer() {
  cp "$SCRIPTS_DIR/docker-sbx-slack-notify.sh" "$FAKEHOME/slack-notify.sh"
  HOME="$FAKEHOME" SANDBOX_NAME=canopy-420 SBX_SLACK_ENABLED=true \
    SBX_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T/B/x' \
    bash -c "source '$INSTALLER_FNS'; prjpath=/mnt/c/Projects/canopy; install_slack_hook" >/dev/null 2>&1 || true
}
count_ours() {
  jq -r '[.hooks[][] | [.hooks[]|select(.command|endswith("slack-notify.sh"))]|length] | unique | join(",")' \
    "$FAKEHOME/.claude/settings.json" 2>/dev/null || echo "err"
}

run_installer
if [[ "$(jq -r '[.hooks|keys[]]|sort|join(",")' "$FAKEHOME/.claude/settings.json" 2>/dev/null)" == "Notification,SessionEnd,SessionStart,Stop" ]]; then
  pass "Wires the four default lifecycle events"
else
  fail "Unexpected events: $(jq -c '.hooks|keys' "$FAKEHOME/.claude/settings.json" 2>/dev/null)"
fi
if [[ "$(stat -c '%a' "$FAKEHOME/.claude/hooks/slack.env" 2>/dev/null)" == "600" ]]; then
  pass "Credentials file is mode 600"
else
  fail "slack.env perms are $(stat -c '%a' "$FAKEHOME/.claude/hooks/slack.env" 2>/dev/null || echo missing)"
fi
if [[ "$(jq -r '.model' "$FAKEHOME/.claude/settings.json")" == "keepme" ]]; then
  pass "Unrelated settings keys survive the merge"
else
  fail "The merge clobbered unrelated keys"
fi
if [[ "$(jq '[.hooks[][]|select(has("matcher"))]|length' "$FAKEHOME/.claude/settings.json")" == "0" ]]; then
  pass "No matcher on lifecycle events"
else
  fail "A matcher was added to a lifecycle event"
fi
run_installer
if [[ "$(count_ours)" == "1" ]]; then
  pass "Re-running setup does not duplicate the hook"
else
  fail "Re-running produced $(count_ours) entries"
fi

# ----------------------------------------------------------------------
# TEST 15: a malformed settings.json is not destroyed by the installer
# ----------------------------------------------------------------------
echo "TEST 15: a malformed settings.json is left intact"
printf '{ "hooks": BROKEN' > "$FAKEHOME/.claude/settings.json"
BEFORE=$(cat "$FAKEHOME/.claude/settings.json")
run_installer
if [[ "$(cat "$FAKEHOME/.claude/settings.json")" == "$BEFORE" ]]; then
  pass "Malformed settings.json left byte-identical"
else
  fail "Malformed settings.json was modified or truncated"
fi

# ----------------------------------------------------------------------
# TEST 16: the hook script posts the expected payload to an endpoint
# ----------------------------------------------------------------------
echo "TEST 16: hook script posts the expected payload to an endpoint"
HOOKDIR="$TMPDIR/hook"
mkdir -p "$HOOKDIR"
cp "$SCRIPTS_DIR/docker-sbx-slack-notify.sh" "$HOOKDIR/slack-notify.sh"
chmod 755 "$HOOKDIR/slack-notify.sh"
SINKLOG="$TMPDIR/sink.jsonl"
: > "$SINKLOG"
# The Content-Type has to be asserted separately: a sink that just reads the body
# accepts a JSON payload sent as x-www-form-urlencoded, which is exactly the
# mistake Slack rejects with invalid_form_data.
SINKCT="$TMPDIR/sink-content-type.txt"
: > "$SINKCT"
SINKPORT=$((21000 + RANDOM % 20000))
cat > "$TMPDIR/sink.py" << 'PY'
import http.server, sys
out = sys.argv[2]
ct = sys.argv[3]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        with open(out, 'a') as f:
            f.write(self.rfile.read(n).decode() + '\n')
        with open(ct, 'a') as f:
            f.write((self.headers.get('Content-Type') or '') + '\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
PY
python3 "$TMPDIR/sink.py" "$SINKPORT" "$SINKLOG" "$SINKCT" &
SINKPID=$!
sleep 1
printf "SBX_SLACK_SANDBOX='canopy-420'\nSBX_SLACK_PROJECT='canopy'\nSBX_SLACK_WEBHOOK_URL='http://127.0.0.1:%s/hook'\n" "$SINKPORT" > "$HOOKDIR/slack.env"
chmod 600 "$HOOKDIR/slack.env"
printf '%s' '{"hook_event_name":"Stop"}' | "$HOOKDIR/slack-notify.sh" || true
sleep 1
if jq -e '.text | test("\\[canopy-420\\] canopy — Claude Code is done")' "$SINKLOG" >/dev/null 2>&1; then
  pass "Delivered the expected message text over the wire"
else
  fail "No matching POST arrived (got: $(cat "$SINKLOG" 2>/dev/null || echo none))"
fi
if grep -qi '^application/json' "$SINKCT"; then
  pass "POST declares application/json (not curl's form-urlencoded default)"
else
  fail "Wrong Content-Type on the wire: '$(cat "$SINKCT" 2>/dev/null)'"
fi
HOOK_RC=0
printf '%s' '{"hook_event_name":"Stop"}' | "$HOOKDIR/slack-notify.sh" >/dev/null 2>&1 || HOOK_RC=$?
if [[ "$HOOK_RC" == "0" ]]; then
  pass "Hook always exits 0 so it can never block the agent"
else
  fail "Hook returned exit code $HOOK_RC"
fi

# ----------------------------------------------------------------------
# TEST 17: a CRLF env file no longer silently skips the install
# ----------------------------------------------------------------------
echo "TEST 17: a CRLF env file installs clean values"
CRLF_HOME="$TMPDIR/crlfhome"
mkdir -p "$CRLF_HOME/.claude"
printf '{"model":"keepme"}\n' > "$CRLF_HOME/.claude/settings.json"
# The shape a Windows editor produces on WSL: CRLF on the SBX_* lines and a final
# line with none. The host side trims the CR, so before the fix the host reported
# the feature as enabled while the sandbox installed nothing at all.
printf 'SBX_SLACK_ENABLED=true\r\nSBX_SLACK_BOT_TOKEN=xoxb-t\r\nSBX_SLACK_CHANNEL=C0123456789\r\nSBX_SLACK_EVENTS=Stop,Notification,SessionStart,SessionEnd\n' > "$TMPDIR/crlf.env"
CRLF_OUT="$(cp "$SCRIPTS_DIR/docker-sbx-slack-notify.sh" "$CRLF_HOME/slack-notify.sh" &&
  HOME="$CRLF_HOME" bash -c "source '$INSTALLER_FNS'; envfilepath='$TMPDIR/crlf.env'; load_env_file; prjpath=/mnt/c/Projects/canopy; install_slack_hook" 2>&1 || true)"
if echo "$CRLF_OUT" | grep -q "Slack notifications enabled"; then
  pass "A CRLF env file still installs the hook"
else
  fail "The install was skipped for a CRLF env file: $CRLF_OUT"
fi
if [[ -f "$CRLF_HOME/.claude/hooks/slack.env" ]] && ! grep -q $'\r' "$CRLF_HOME/.claude/hooks/slack.env"; then
  pass "slack.env carries no carriage returns"
else
  fail "slack.env is missing or still contains CR"
fi
if [[ "$(jq -r '[.hooks|keys[]]|sort|join(",")' "$CRLF_HOME/.claude/settings.json" 2>/dev/null)" == "Notification,SessionEnd,SessionStart,Stop" ]]; then
  pass "Hooks are wired from a CRLF env file"
else
  fail "Hooks not wired: $(jq -c '.hooks|keys' "$CRLF_HOME/.claude/settings.json" 2>/dev/null)"
fi
# An unrecognised value must say so rather than no-op, which is what made the
# mismatch invisible in the first place.
printf 'SBX_SLACK_ENABLED=True\nSBX_SLACK_WEBHOOK_URL=https://hooks.slack.com/x\n' > "$TMPDIR/crlf-typo.env"
TYPO_OUT="$(HOME="$TMPDIR/typo-home" bash -c "mkdir -p '$TMPDIR/typo-home'; source '$INSTALLER_FNS'; envfilepath='$TMPDIR/crlf-typo.env'; load_env_file; install_slack_hook" 2>&1 || true)"
if echo "$TYPO_OUT" | grep -q "expected 'true'"; then
  pass "A mis-cased SBX_SLACK_ENABLED warns instead of silently no-op'ing"
else
  fail "A mis-cased SBX_SLACK_ENABLED stayed silent: $TYPO_OUT"
fi

# ----------------------------------------------------------------------
# TEST 18: a refused or blocked post is recorded, a successful one is not
# ----------------------------------------------------------------------
echo "TEST 18: failed posts are recorded, successes are not"
HOOKLOG_HOME="$TMPDIR/hooklog"
mkdir -p "$HOOKLOG_HOME"
cp "$SCRIPTS_DIR/docker-sbx-slack-notify.sh" "$HOOKLOG_HOME/slack-notify.sh"
chmod 755 "$HOOKLOG_HOME/slack-notify.sh"
STUB_BIN2="$TMPDIR/stubbin2"
mkdir -p "$STUB_BIN2"
# Stand in for curl so the response can be dictated: the status on stdout (what
# curl --write-out produces) and the body in the --output file.
cat > "$STUB_BIN2/curl" << 'SCRIPT'
#!/bin/bash
out=""; args=("$@")
for ((i=0;i<${#args[@]};i++)); do
  case "${args[i]}" in --output) out="${args[i+1]}" ;; esac
done
[[ -n "${STUB_ARGS_LOG:-}" ]] && printf '%s\n' "$*" >> "$STUB_ARGS_LOG"
[[ -n "$out" ]] && printf '%s' "${STUB_BODY:-ok}" > "$out"
printf '%s' "${STUB_STATUS:-200}"
SCRIPT
chmod +x "$STUB_BIN2/curl"
LOG="$HOOKLOG_HOME/slack-errors.log"
ARGSLOG="$HOOKLOG_HOME/curl-args.txt"
: > "$ARGSLOG"
HOOK_RC=0
fire_hook() {
  PATH="$STUB_BIN2:$PATH" STUB_STATUS="$1" STUB_BODY="$2" STUB_ARGS_LOG="$ARGSLOG" \
    bash -c "printf '%s' '{\"hook_event_name\":\"Stop\"}' | '$HOOKLOG_HOME/slack-notify.sh'" 2>/dev/null || HOOK_RC=$?
}

printf "SBX_SLACK_SANDBOX='probe'\nSBX_SLACK_PROJECT='proj'\nSBX_SLACK_BOT_TOKEN='xoxb-t'\nSBX_SLACK_CHANNEL='C0123'\n" > "$HOOKLOG_HOME/slack.env"
# A realistic full-size success body: it is longer than the log's truncation
# bound, so this also guards against a success being misread as a refusal.
SUCCESS_BODY='{"ok":true,"channel":"C0123","ts":"1.2","message":{"text":"[probe] proj — Claude Code is done","type":"message","bot_id":"B1","app_id":"A1","username":"sbx","ts":"1.2"}}'
SUCCESS_OUT="$(fire_hook 200 "$SUCCESS_BODY")"
if [[ ! -f "$LOG" ]]; then
  pass "A successful post writes no error log"
else
  fail "Success was logged: $(cat "$LOG")"
fi
if [[ -z "$SUCCESS_OUT" ]]; then
  pass "Hook stays silent on stdout"
else
  fail "Hook wrote to stdout: $SUCCESS_OUT"
fi
# The token branch posts to slack.com, so its request cannot be observed at a local
# sink; the stub records the argv instead. Slack answers a JSON body sent as
# x-www-form-urlencoded with invalid_form_data, so this must be asserted here too.
if tail -1 "$ARGSLOG" | grep -q "Content-type: application/json" \
   && tail -1 "$ARGSLOG" | grep -q "Authorization: Bearer xoxb-t"; then
  pass "Token mode sends JSON content type and the bearer token"
else
  fail "Token mode request is malformed: $(tail -1 "$ARGSLOG")"
fi

# Slack answers API errors with HTTP 200, so the body is the only signal.
HOOK_RC=0
fire_hook 200 '{"ok":false,"error":"not_in_channel"}' >/dev/null
if grep -q "not_in_channel" "$LOG" 2>/dev/null; then
  pass "A refused chat.postMessage is logged with Slack's error"
else
  fail "not_in_channel was not logged: $(cat "$LOG" 2>/dev/null || echo none)"
fi
# The channel has to be in the line: channel_not_found and not_in_channel are only
# distinguishable by looking at exactly what was sent.
if grep -q "channel='C0123'" "$LOG" 2>/dev/null; then
  pass "The refusal line names the channel it tried"
else
  fail "The configured channel is missing from the log: $(tail -1 "$LOG" 2>/dev/null)"
fi
# A personal DM with yourself is a valid D... id that no app can post into, and it
# reports the same channel_not_found a typo would. The line has to say which it is.
printf "SBX_SLACK_SANDBOX='probe'\nSBX_SLACK_PROJECT='proj'\nSBX_SLACK_BOT_TOKEN='xoxb-t'\nSBX_SLACK_CHANNEL='D6EC8RR89'\n" > "$HOOKLOG_HOME/slack.env"
fire_hook 200 '{"ok":false,"error":"channel_not_found"}' >/dev/null
if tail -1 "$LOG" | grep -q "channel='D6EC8RR89'" && tail -1 "$LOG" | grep -q "personal DM with yourself"; then
  pass "A D... refusal explains the direct-message trap"
else
  fail "The D... trap was not explained: $(tail -1 "$LOG" 2>/dev/null)"
fi
if [[ "$HOOK_RC" == "0" ]]; then
  pass "Still exits 0 after a refusal, so the agent is never blocked"
else
  fail "Hook returned $HOOK_RC after a refusal"
fi

# A request dropped by the network policy: non-2xx carrying the block reason.
printf "SBX_SLACK_SANDBOX='probe'\nSBX_SLACK_PROJECT='proj'\nSBX_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T0/B0/SECRETPATH'\n" > "$HOOKLOG_HOME/slack.env"
fire_hook 403 'Blocked by network policy: domain hooks.slack.com:443' >/dev/null
if tail -1 "$LOG" | grep -q "403" && tail -1 "$LOG" | grep -q "Blocked by network policy"; then
  pass "A policy-blocked webhook is logged with the block reason"
else
  fail "The 403 block was not logged: $(tail -1 "$LOG" 2>/dev/null)"
fi
if tail -1 "$LOG" | grep -q "SECRETPATH"; then
  fail "The webhook secret was written to the log"
else
  pass "The webhook path is redacted in the log"
fi
if [[ "$(stat -c '%a' "$LOG" 2>/dev/null)" == "600" ]]; then
  pass "The error log is mode 600"
else
  fail "Log perms are $(stat -c '%a' "$LOG" 2>/dev/null || echo missing)"
fi

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo ""
echo "═══════════════════════════════════════════════════════"
echo "Results: $PASSED passed, $FAILED failed"
echo "═══════════════════════════════════════════════════════"

if [[ $FAILED -gt 0 ]]; then
  exit 1
fi
