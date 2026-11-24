#!/bin/bash
set -euo pipefail

# Locate the scripts relative to this file so the suite runs from any checkout.
# The suite lives in test/, so the repository root - where the host scripts and
# sandbox/ are - is one level up. Earlier this was a hard-coded absolute path,
# which made every test that invokes "$SCRIPT" fail anywhere but the author's
# machine - and, worse, the two tests that *expect* a non-zero exit (missing -sf
# file, -gsf to a bad directory) passed for the wrong reason, since a missing
# script exits non-zero too. Fail loudly rather than quietly skew the results.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The migration from bash to cross-platform Python is done, so the CLI-level tests
# below run against the Python implementation; the bash host script
# (docker-sbx-create-sandbox.sh) is no longer in the tree. While both existed the
# same assertions ran against each, which is what caught them drifting apart - the
# override is kept for a checkout that still has both:
#   SBX_IMPLS=python bash test/run-tests.sh
IMPLS="${SBX_IMPLS:-python}"

select_impl() {
  case "$1" in
    # Only reachable with SBX_IMPLS=bash, and only where the retired script is still
    # present; otherwise the check below fails loudly.
    bash)   SCRIPT="$REPO_DIR/docker-sbx-create-sandbox.sh"; RUNNER=(bash) ;;
    python) SCRIPT="$REPO_DIR/docker-sbx-create-sandbox.py"; RUNNER=(python3) ;;
    *) echo "unknown implementation: $1 (expected bash or python)" >&2; exit 2 ;;
  esac
  IMPL="$1"
  if [[ ! -f "$SCRIPT" ]]; then
    echo "Error: $SCRIPT not found - is this suite still inside the repository?" >&2
    exit 1
  fi
}

# Invoke the implementation under test. RUNNER is an array so `bash file.sh` and
# `python3 file.py` are handled uniformly.
run_create() { "${RUNNER[@]}" "$SCRIPT" "$@"; }

TMPDIR="/tmp/docker-sbx-tests-$$"
PASSED=0
FAILED=0

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
pass() { echo "  [PASS] $1"; ((PASSED++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAILED++)) || true; }

cleanup() {
  for pid in "${SINKPID:-}" "${NCTPID:-}"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TMPDIR"

# ----------------------------------------------------------------------
# CLI-level tests (1-12): every one of these drives the implementation under
# test through its command line, so they are run once per implementation.
# ----------------------------------------------------------------------
cli_tests() {

# ----------------------------------------------------------------------
# TEST 1: -gsf generates a valid JSON secrets config file
# ----------------------------------------------------------------------
echo "TEST 1: -gsf generates a valid JSON secrets config file"
OUTFILE="$TMPDIR/generated.json"
if run_create -gsf "$OUTFILE" >/dev/null 2>&1; then
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
if run_create -sf "$TMPDIR/nonexistent.json" -n test -p /tmp 2>/dev/null; then
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
OUTPUT=$(run_create -h 2>&1 || true)
if echo "$OUTPUT" | grep -q "Usage:" && echo "$OUTPUT" | grep -q "\-sf"; then
  pass "Help output contains usage and -sf option"
else
  fail "Help output missing usage or -sf option"
fi

# ----------------------------------------------------------------------
# TEST 7: Unknown option shows error and usage
# ----------------------------------------------------------------------
echo "TEST 7: Unknown option shows error and usage"
OUTPUT=$(run_create --unknown 2>&1 || true)
if echo "$OUTPUT" | grep -q "Unknown option" && echo "$OUTPUT" | grep -q "Usage:"; then
  pass "Unknown option handled correctly"
else
  fail "Unknown option not handled correctly"
fi

# ----------------------------------------------------------------------
# TEST 8: Missing mandatory params shows error
# ----------------------------------------------------------------------
echo "TEST 8: Missing mandatory params shows error"
OUTPUT=$(run_create 2>&1 || true)
if echo "$OUTPUT" | grep -q "Missing parameter"; then
  pass "Missing params error shown"
else
  fail "Missing params error not shown"
fi

# ----------------------------------------------------------------------
# TEST 9: -gsf to nonexistent directory fails
# ----------------------------------------------------------------------
echo "TEST 9: -gsf to nonexistent directory fails"
if run_create -gsf "$TMPDIR/no/dir/file.json" 2>/dev/null; then
  fail "Should have failed for nonexistent directory"
else
  pass "Correctly failed for nonexistent directory"
fi

# ----------------------------------------------------------------------
# TEST 10: Generated JSON contains expected default patterns
# ----------------------------------------------------------------------
echo "TEST 10: Generated JSON contains expected default patterns"
GENFILE="$TMPDIR/gen-verify.json"
run_create -gsf "$GENFILE" >/dev/null 2>&1 || true
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
OUTPUT=$(run_create -n test -p /tmp -s false -e "$SLACK_ENV" 2>&1 || true)
if echo "$OUTPUT" | grep -q "no usable Slack target"; then
  pass "Aborts with a clear message when no target is configured"
else
  fail "Did not report the missing Slack target"
fi
if run_create -n test -p /tmp -s false -e "$SLACK_ENV" >/dev/null 2>&1; then
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
OUTPUT=$(run_create -n test -p /tmp -s false -e "$BAD_EVENT_ENV" 2>&1 || true)
if echo "$OUTPUT" | grep -q "unsupported SBX_SLACK_EVENTS entry 'Bogus'"; then
  pass "Rejects an unknown event name"
else
  fail "Did not reject the unknown event name"
fi
}

for impl in $IMPLS; do
  select_impl "$impl"
  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "Implementation under test: $IMPL"
  echo "═══════════════════════════════════════════════════════"
  cli_tests
done

# ----------------------------------------------------------------------
# Implementation-independent tests (13-19): these exercise the in-sandbox
# scripts (sandbox/*.sh) rather than a host implementation, so they run once.
# TEST 13 drives the host's Slack policy code, which is Python now - the bash host
# script it used to extract shell functions from was retired - so SCRIPT is
# re-pointed at the module explicitly instead of at whatever the loop selected.
# ----------------------------------------------------------------------
SCRIPT="$REPO_DIR/docker-sbx-create-sandbox.py"
SCRIPTS_DIR="$REPO_DIR"

# ----------------------------------------------------------------------
# TEST 13: host side allows only the endpoints the credential style needs
# ----------------------------------------------------------------------
echo "TEST 13: host side allows only the needed Slack endpoints"
STUB_BIN="$TMPDIR/stubbin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/sbx" << 'SCRIPT'
#!/bin/bash
printf 'sbx %s\n' "$*"
SCRIPT
chmod +x "$STUB_BIN/sbx"

hosts_for() {
  # Import the host module, point its CONFIG at the case's env file and run
  # update_sbx_policy_slack() with the stub `sbx` on PATH; read back the hosts it
  # allowed. (The Python port echoes each command it runs, so the "allowing" lines
  # are what identify the policy rules actually applied.)
  PATH="$STUB_BIN:$PATH" python3 - "$SCRIPT" "$1" 2>&1 <<'PY' \
    | grep -o 'allowing .*' | sed 's/allowing //' | paste -sd, - || true
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("create_sbx", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.CONFIG.name = "probe"
module.CONFIG.envfile = sys.argv[2]
module.update_sbx_policy_slack()
PY
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
sed -n '/^load_env_file() {/,/^update_claude_code_settings() {/p' "$SCRIPTS_DIR/sandbox/docker-sbx-setup-sandbox.sh" | head -n -1 > "$INSTALLER_FNS"
FAKEHOME="$TMPDIR/fakehome"
mkdir -p "$FAKEHOME/.claude"
printf '{"model":"keepme"}\n' > "$FAKEHOME/.claude/settings.json"

run_installer() {
  cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$FAKEHOME/slack-notify.sh"
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
cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$HOOKDIR/slack-notify.sh"
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
CRLF_OUT="$(cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$CRLF_HOME/slack-notify.sh" &&
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
cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$HOOKLOG_HOME/slack-notify.sh"
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
config="$(cat)"
[[ -n "${STUB_ARGS_LOG:-}" ]] && printf '%s\n' "$*" >> "$STUB_ARGS_LOG"
[[ -n "${STUB_CONFIG_LOG:-}" ]] && printf '%s\n' "$config" >> "$STUB_CONFIG_LOG"
[[ -n "$out" ]] && printf '%s' "${STUB_BODY:-ok}" > "$out"
printf '%s' "${STUB_STATUS:-200}"
SCRIPT
chmod +x "$STUB_BIN2/curl"
LOG="$HOOKLOG_HOME/slack-errors.log"
ARGSLOG="$HOOKLOG_HOME/curl-args.txt"
CONFIGLOG="$HOOKLOG_HOME/curl-config.txt"
: > "$ARGSLOG"
: > "$CONFIGLOG"
HOOK_RC=0
fire_hook() {
  PATH="$STUB_BIN2:$PATH" STUB_STATUS="$1" STUB_BODY="$2" STUB_ARGS_LOG="$ARGSLOG" STUB_CONFIG_LOG="$CONFIGLOG" \
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
# sink; the stub records argv and the curl config received on stdin instead. Slack
# answers a JSON body sent as x-www-form-urlencoded with invalid_form_data, so the
# content type must be asserted here too.
if tail -1 "$ARGSLOG" | grep -q "Content-type: application/json" \
   && grep -qF 'header = "Authorization: Bearer xoxb-t"' "$CONFIGLOG"; then
  pass "Token mode sends JSON content type and the bearer token"
else
  fail "Token mode request is malformed: $(tail -1 "$ARGSLOG") / $(tail -2 "$CONFIGLOG")"
fi
if grep -q 'xoxb-t' "$ARGSLOG"; then
  fail "The Slack bot token was exposed in curl's arguments"
else
  pass "The Slack bot token stays out of curl's arguments"
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
if grep -q 'SECRETPATH' "$ARGSLOG"; then
  fail "The webhook secret was exposed in curl's arguments"
elif grep -qF 'url = "https://hooks.slack.com/services/T0/B0/SECRETPATH"' "$CONFIGLOG"; then
  pass "The secret webhook URL reaches curl through stdin, not argv"
else
  fail "The webhook URL was missing from curl's stdin configuration"
fi
if [[ "$(stat -c '%a' "$LOG" 2>/dev/null)" == "600" ]]; then
  pass "The error log is mode 600"
else
  fail "Log perms are $(stat -c '%a' "$LOG" 2>/dev/null || echo missing)"
fi

# ----------------------------------------------------------------------
# TEST 19: the settings merge writes the opt-outs, and invents nothing
# ----------------------------------------------------------------------
echo "TEST 19: the settings merge writes the opt-outs and nothing invented"
MERGE_FNS="$TMPDIR/merge-fns.sh"
# set_env_if_missing through update_claude_code_settings: the merge needs both.
sed -n '/^set_env_if_missing() {/,/^setup_env() {/p' "$SCRIPTS_DIR/sandbox/docker-sbx-setup-sandbox.sh" | head -n -1 > "$MERGE_FNS"
MERGE_HOME="$TMPDIR/merge-home"
mkdir -p "$MERGE_HOME/.claude"
printf '{}\n' > "$MERGE_HOME/.claude/settings.json"

merge_env() { jq -r --arg k "$1" '.env[$k] // "absent"' "$MERGE_HOME/.claude/settings.json" 2>/dev/null; }
run_merge() {
  # These three are cleared: the merge passes a value through when one is already
  # exported, so an ambient copy would decide the "invents nothing" checks below
  # instead of the script.
  HOME="$MERGE_HOME" env -u CLAUDE_CODE_ATTRIBUTION_HEADER -u CLAUDE_CODE_ENABLE_TELEMETRY \
    -u CLAUDE_CODE_DISABLE_TELEMETRY bash -c "source '$MERGE_FNS'; update_claude_code_settings" >/dev/null 2>&1 || true
}

run_merge
if [[ "$(merge_env DISABLE_TELEMETRY)" == "1" && "$(merge_env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)" == "1" ]]; then
  pass "Writes the two documented opt-outs"
else
  fail "Opt-outs missing: $(jq -c '.env' "$MERGE_HOME/.claude/settings.json" 2>/dev/null)"
fi
# Each of these was either not a Claude Code variable or a value that says nothing.
for key in CLAUDE_CODE_DISABLE_TELEMETRY CLAUDE_CODE_ENABLE_TELEMETRY CLAUDE_CODE_ATTRIBUTION_HEADER; do
  if [[ "$(merge_env "$key")" == "absent" ]]; then
    pass "Does not invent $key"
  else
    fail "$key was written as '$(merge_env "$key")'"
  fi
done
# ...but a value the env file supplies is still carried through.
HOME="$MERGE_HOME" CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
  bash -c "source '$MERGE_FNS'; update_claude_code_settings" >/dev/null 2>&1 || true
if [[ "$(merge_env CLAUDE_CODE_ATTRIBUTION_HEADER)" == "0" ]]; then
  pass "Passes a supplied CLAUDE_CODE_ATTRIBUTION_HEADER through"
else
  fail "A supplied CLAUDE_CODE_ATTRIBUTION_HEADER was dropped"
fi

# ----------------------------------------------------------------------
# TEST 20: host side allows the Nextcloud Talk instance's host
# ----------------------------------------------------------------------
echo "TEST 20: host side allows the Nextcloud Talk host"
nctalk_hosts_for() {
  # Mirror hosts_for(), driving update_sbx_policy_nctalk() instead.
  PATH="$STUB_BIN:$PATH" python3 - "$SCRIPT" "$1" 2>&1 <<'PY' \
    | grep -o 'allowing .*' | sed 's/allowing //' | paste -sd, - || true
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("create_sbx", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.CONFIG.name = "probe"
module.CONFIG.envfile = sys.argv[2]
module.update_sbx_policy_nctalk()
PY
}
NC_CASE_ENV="$TMPDIR/nctalk-hosts.env"
printf 'SBX_NCTALK_ENABLED=true\nSBX_NCTALK_URL=https://cloud.example.com\nSBX_NCTALK_USER=a\nSBX_NCTALK_TOKEN=t\nSBX_NCTALK_ROOM=r\n' > "$NC_CASE_ENV"
if [[ "$(nctalk_hosts_for "$NC_CASE_ENV")" == "cloud.example.com" ]]; then
  pass "The Talk instance host is allowed"
else
  fail "Talk mode allowed '$(nctalk_hosts_for "$NC_CASE_ENV")'"
fi
printf 'SBX_NCTALK_ENABLED=false\nSBX_NCTALK_URL=https://cloud.example.com\n' > "$NC_CASE_ENV"
if [[ -z "$(nctalk_hosts_for "$NC_CASE_ENV")" ]]; then
  pass "Disabled Talk adds no policy rule at all"
else
  fail "Disabled Talk still added '$(nctalk_hosts_for "$NC_CASE_ENV")'"
fi

# ----------------------------------------------------------------------
# TEST 21: the in-sandbox Talk installer writes nctalk.env and wires hooks
# ----------------------------------------------------------------------
echo "TEST 21: the Talk installer writes its config and wires hooks"
NCT_HOME="$TMPDIR/nctalk-home"
mkdir -p "$NCT_HOME/.claude"
printf '{"model":"keepme"}\n' > "$NCT_HOME/.claude/settings.json"
cp "$SCRIPTS_DIR/sandbox/docker-sbx-nctalk-notify.sh" "$NCT_HOME/nctalk-notify.sh"
HOME="$NCT_HOME" SANDBOX_NAME=nc-420 SBX_NCTALK_ENABLED=true \
  SBX_NCTALK_URL='https://cloud.example.com' SBX_NCTALK_USER='agent' \
  SBX_NCTALK_TOKEN='app-pw' SBX_NCTALK_ROOM='roomtoken' \
  bash -c "source '$INSTALLER_FNS'; prjpath=/mnt/c/Projects/ncproj; install_nctalk_hook" >/dev/null 2>&1 || true
if [[ "$(jq -r '[.hooks|keys[]]|sort|join(",")' "$NCT_HOME/.claude/settings.json" 2>/dev/null)" == "Notification,SessionEnd,SessionStart,Stop" ]]; then
  pass "Wires the four default lifecycle events for Talk"
else
  fail "Unexpected events: $(jq -c '.hooks|keys' "$NCT_HOME/.claude/settings.json" 2>/dev/null)"
fi
if [[ "$(stat -c '%a' "$NCT_HOME/.claude/hooks/nctalk.env" 2>/dev/null)" == "600" ]]; then
  pass "nctalk.env is mode 600"
else
  fail "nctalk.env perms are $(stat -c '%a' "$NCT_HOME/.claude/hooks/nctalk.env" 2>/dev/null || echo missing)"
fi
if grep -q "SBX_NCTALK_ROOM='roomtoken'" "$NCT_HOME/.claude/hooks/nctalk.env" 2>/dev/null \
   && grep -q "SBX_NCTALK_URL='https://cloud.example.com'" "$NCT_HOME/.claude/hooks/nctalk.env" 2>/dev/null; then
  pass "nctalk.env carries the target under the SBX_NCTALK_ prefix"
else
  fail "nctalk.env is missing its target: $(cat "$NCT_HOME/.claude/hooks/nctalk.env" 2>/dev/null || echo none)"
fi

# ----------------------------------------------------------------------
# TEST 22: the Talk hook posts to an endpoint with the OCS contract
# ----------------------------------------------------------------------
echo "TEST 22: the Talk hook posts to an endpoint with the OCS contract"
NCHOOK="$TMPDIR/nchook"
mkdir -p "$NCHOOK"
cp "$SCRIPTS_DIR/sandbox/docker-sbx-nctalk-notify.sh" "$NCHOOK/nctalk-notify.sh"
chmod 755 "$NCHOOK/nctalk-notify.sh"
NCTLOG="$TMPDIR/nct-sink.jsonl"
NCTMETA="$TMPDIR/nct-meta.jsonl"
: > "$NCTLOG"
: > "$NCTMETA"
NCTPORT=$((21000 + RANDOM % 20000))
# A sink that records the body plus the request line and headers, so the path, the
# OCS-APIRequest header and the basic-auth credential can all be asserted.
cat > "$TMPDIR/nct-sink.py" << 'PY'
import http.server, json, sys
out, meta = sys.argv[2], sys.argv[3]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n).decode()
        with open(out, 'a') as f:
            f.write(body + '\n')
        with open(meta, 'a') as f:
            f.write(json.dumps({
                'path': self.path,
                'content_type': self.headers.get('Content-Type') or '',
                'ocs': self.headers.get('OCS-APIRequest') or '',
                'authorization': self.headers.get('Authorization') or '',
            }) + '\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
PY
python3 "$TMPDIR/nct-sink.py" "$NCTPORT" "$NCTLOG" "$NCTMETA" &
NCTPID=$!
sleep 1
printf "SBX_NCTALK_SANDBOX='nc-420'\nSBX_NCTALK_PROJECT='ncproj'\nSBX_NCTALK_URL='http://127.0.0.1:%s'\nSBX_NCTALK_USER='agent'\nSBX_NCTALK_TOKEN='app-pw'\nSBX_NCTALK_ROOM='roomtoken'\n" "$NCTPORT" > "$NCHOOK/nctalk.env"
chmod 600 "$NCHOOK/nctalk.env"
printf '%s' '{"hook_event_name":"Stop"}' | "$NCHOOK/nctalk-notify.sh" || true
sleep 1
if jq -e '.message | test("\\[nc-420\\] ncproj — Claude Code is done")' "$NCTLOG" >/dev/null 2>&1; then
  pass "Delivered the expected Talk message text over the wire"
else
  fail "No matching Talk POST arrived (got: $(cat "$NCTLOG" 2>/dev/null || echo none))"
fi
if tail -1 "$NCTMETA" | jq -re '.path | endswith("/ocs/v2.php/apps/spreed/api/v1/chat/roomtoken")' >/dev/null 2>&1; then
  pass "Posts to the spreed chat endpoint for the configured room"
else
  fail "Unexpected Talk path: $(tail -1 "$NCTMETA" 2>/dev/null)"
fi
if tail -1 "$NCTMETA" | jq -re '.ocs == "true" and (.content_type | startswith("application/json"))' >/dev/null 2>&1; then
  pass "Talk POST declares OCS-APIRequest and application/json"
else
  fail "Wrong Talk headers: $(tail -1 "$NCTMETA" 2>/dev/null)"
fi
if tail -1 "$NCTMETA" | jq -re '.authorization | startswith("Basic ")' >/dev/null 2>&1; then
  pass "Talk POST carries the app password as basic auth"
else
  fail "Missing basic-auth credential: $(tail -1 "$NCTMETA" 2>/dev/null)"
fi

# ----------------------------------------------------------------------
# TEST 23: Slack and Nextcloud Talk can be enabled together
# ----------------------------------------------------------------------
echo "TEST 23: Slack and Talk can be enabled together"
BOTH_HOME="$TMPDIR/bothhome"
mkdir -p "$BOTH_HOME/.claude"
printf '{}\n' > "$BOTH_HOME/.claude/settings.json"
run_both() {
  cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$BOTH_HOME/slack-notify.sh"
  cp "$SCRIPTS_DIR/sandbox/docker-sbx-nctalk-notify.sh" "$BOTH_HOME/nctalk-notify.sh"
  HOME="$BOTH_HOME" SANDBOX_NAME=both SBX_SLACK_ENABLED=true \
    SBX_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T/B/x' \
    SBX_NCTALK_ENABLED=true SBX_NCTALK_URL='https://cloud.example.com' \
    SBX_NCTALK_USER='agent' SBX_NCTALK_TOKEN='app-pw' SBX_NCTALK_ROOM='roomtoken' \
    bash -c "source '$INSTALLER_FNS'; prjpath=/mnt/c/Projects/both; install_slack_hook; install_nctalk_hook" >/dev/null 2>&1 || true
}
per_event_commands() {
  jq -r '[.hooks[] | [.[].hooks[].command] | length] | unique | join(",")' \
    "$BOTH_HOME/.claude/settings.json" 2>/dev/null || echo err
}
run_both
if [[ "$(per_event_commands)" == "2" ]]; then
  pass "Every event carries one hook entry per provider"
else
  fail "Unexpected hooks per event: $(jq -c '.hooks' "$BOTH_HOME/.claude/settings.json" 2>/dev/null)"
fi
if jq -e '[.hooks[][] | .hooks[].command] | (any(endswith("slack-notify.sh"))) and (any(endswith("nctalk-notify.sh")))' \
     "$BOTH_HOME/.claude/settings.json" >/dev/null 2>&1; then
  pass "Both hook scripts are wired into settings.json"
else
  fail "One provider was lost: $(jq -c '.hooks' "$BOTH_HOME/.claude/settings.json" 2>/dev/null)"
fi
run_both
if [[ "$(per_event_commands)" == "2" ]]; then
  pass "Re-running setup with both providers stays idempotent"
else
  fail "Re-running duplicated hooks: $(jq -c '.hooks' "$BOTH_HOME/.claude/settings.json" 2>/dev/null)"
fi

# ----------------------------------------------------------------------
# TEST 24: a refused Talk post is recorded, a successful one is not
# ----------------------------------------------------------------------
echo "TEST 24: a refused Talk post is recorded, a successful one is not"
NCLOG_HOME="$TMPDIR/nclog"
mkdir -p "$NCLOG_HOME"
cp "$SCRIPTS_DIR/sandbox/docker-sbx-nctalk-notify.sh" "$NCLOG_HOME/nctalk-notify.sh"
chmod 755 "$NCLOG_HOME/nctalk-notify.sh"
STUB_BIN3="$TMPDIR/stubbin3"
mkdir -p "$STUB_BIN3"
# Stand in for curl so the response can be dictated: the status on stdout (what
# curl --write-out produces) and the body in the --output file.
cat > "$STUB_BIN3/curl" << 'SCRIPT'
#!/bin/bash
out=""; args=("$@")
for ((i=0;i<${#args[@]};i++)); do
  case "${args[i]}" in --output) out="${args[i+1]}" ;; esac
done
config="$(cat)"
[[ -n "${STUB_ARGS_LOG:-}" ]] && printf '%s\n' "$*" >> "$STUB_ARGS_LOG"
[[ -n "${STUB_CONFIG_LOG:-}" ]] && printf '%s\n' "$config" >> "$STUB_CONFIG_LOG"
[[ -n "$out" ]] && printf '%s' "${STUB_BODY:-ok}" > "$out"
printf '%s' "${STUB_STATUS:-200}"
SCRIPT
chmod +x "$STUB_BIN3/curl"
NCLOG="$NCLOG_HOME/nctalk-errors.log"
NCARGSLOG="$NCLOG_HOME/curl-args.txt"
NCCONFIGLOG="$NCLOG_HOME/curl-config.txt"
: > "$NCARGSLOG"
: > "$NCCONFIGLOG"
fire_nctalk() {
  PATH="$STUB_BIN3:$PATH" STUB_STATUS="$1" STUB_BODY="$2" \
    STUB_ARGS_LOG="$NCARGSLOG" STUB_CONFIG_LOG="$NCCONFIGLOG" \
    bash -c "printf '%s' '{\"hook_event_name\":\"Stop\"}' | '$NCLOG_HOME/nctalk-notify.sh'" 2>/dev/null || true
}
printf "SBX_NCTALK_SANDBOX='probe'\nSBX_NCTALK_PROJECT='proj'\nSBX_NCTALK_URL='https://cloud.example.com'\nSBX_NCTALK_USER='agent'\nSBX_NCTALK_TOKEN='app-pw'\nSBX_NCTALK_ROOM='roomtoken'\n" > "$NCLOG_HOME/nctalk.env"
fire_nctalk 200 '{"ocs":{"meta":{"statuscode":200},"data":{}}}'
if [[ ! -f "$NCLOG" ]]; then
  pass "A successful Talk post writes no error log"
else
  fail "Success was logged: $(cat "$NCLOG")"
fi
if grep -q 'app-pw' "$NCARGSLOG"; then
  fail "The Talk app password was exposed in curl's arguments"
elif grep -qF 'user = "agent:app-pw"' "$NCCONFIGLOG"; then
  pass "The Talk app password reaches curl through stdin, not argv"
else
  fail "The Talk basic-auth credential was missing from curl's stdin configuration"
fi
# A non-2xx OCS reply: the reason must be recorded, and the room named, since a
# wrong room token and a policy-blocked request look alike otherwise.
fire_nctalk 404 '{"ocs":{"meta":{"statuscode":404,"message":"room not found"}}}'
if tail -1 "$NCLOG" | grep -q "404" && tail -1 "$NCLOG" | grep -q "room='roomtoken'"; then
  pass "A refused Talk post is logged with the status and the room"
else
  fail "The Talk refusal was not logged: $(tail -1 "$NCLOG" 2>/dev/null)"
fi
if [[ "$(stat -c '%a' "$NCLOG" 2>/dev/null)" == "600" ]]; then
  pass "The Talk error log is mode 600"
else
  fail "Talk log perms are $(stat -c '%a' "$NCLOG" 2>/dev/null || echo missing)"
fi

# ----------------------------------------------------------------------
# TEST 25: an invalid Nextcloud base URL aborts before sandbox creation
# ----------------------------------------------------------------------
echo "TEST 25: an invalid Nextcloud base URL aborts before sandbox creation"
NC_BAD_URL_ENV="$TMPDIR/nctalk-bad-url.env"
printf 'SBX_NCTALK_ENABLED=true\nSBX_NCTALK_URL=cloud.example.com\nSBX_NCTALK_USER=agent\nSBX_NCTALK_TOKEN=app-pw\nSBX_NCTALK_ROOM=roomtoken\n' > "$NC_BAD_URL_ENV"
OUTPUT=$(python3 "$REPO_DIR/docker-sbx-create-sandbox.py" -n test -p /tmp -s false -e "$NC_BAD_URL_ENV" 2>&1 || true)
if echo "$OUTPUT" | grep -q "SBX_NCTALK_URL must be an absolute http:// or https:// URL"; then
  pass "A URL without an HTTP scheme is rejected with a clear message"
else
  fail "Did not report the invalid Talk URL: $OUTPUT"
fi
if python3 "$REPO_DIR/docker-sbx-create-sandbox.py" -n test -p /tmp -s false -e "$NC_BAD_URL_ENV" >/dev/null 2>&1; then
  fail "Should have exited non-zero for an invalid Talk URL"
else
  pass "Exits non-zero before requiring sbx or creating anything"
fi

# ----------------------------------------------------------------------
# TEST 26: dry-run reports on stderr and never invokes curl
# ----------------------------------------------------------------------
echo "TEST 26: dry-run reports on stderr and never invokes curl"
DRYRUN_ROOT="$TMPDIR/dryrun"
DRYRUN_BIN="$DRYRUN_ROOT/bin"
mkdir -p "$DRYRUN_BIN/slack" "$DRYRUN_BIN/nctalk"
cat > "$DRYRUN_BIN/curl" << 'SCRIPT'
#!/bin/bash
cat >/dev/null
printf 'called\n' >> "$DRY_CALLS"
printf '200'
SCRIPT
chmod +x "$DRYRUN_BIN/curl"
cp "$SCRIPTS_DIR/sandbox/docker-sbx-slack-notify.sh" "$DRYRUN_BIN/slack/slack-notify.sh"
cp "$SCRIPTS_DIR/sandbox/docker-sbx-nctalk-notify.sh" "$DRYRUN_BIN/nctalk/nctalk-notify.sh"
printf "SBX_SLACK_SANDBOX='probe'\nSBX_SLACK_PROJECT='proj'\nSBX_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T/B/x'\nSBX_SLACK_DRYRUN='true'\n" > "$DRYRUN_BIN/slack/slack.env"
printf "SBX_NCTALK_SANDBOX='probe'\nSBX_NCTALK_PROJECT='proj'\nSBX_NCTALK_URL='https://cloud.example.com'\nSBX_NCTALK_USER='agent'\nSBX_NCTALK_TOKEN='app-pw'\nSBX_NCTALK_ROOM='roomtoken'\nSBX_NCTALK_DRYRUN='true'\n" > "$DRYRUN_BIN/nctalk/nctalk.env"
: > "$DRYRUN_BIN/slack.calls"
: > "$DRYRUN_BIN/nctalk.calls"
printf '%s' '{"hook_event_name":"Stop"}' | PATH="$DRYRUN_BIN:$PATH" DRY_CALLS="$DRYRUN_BIN/slack.calls" \
  "$DRYRUN_BIN/slack/slack-notify.sh" > "$DRYRUN_BIN/slack.out" 2> "$DRYRUN_BIN/slack.err"
printf '%s' '{"hook_event_name":"Stop"}' | PATH="$DRYRUN_BIN:$PATH" DRY_CALLS="$DRYRUN_BIN/nctalk.calls" \
  "$DRYRUN_BIN/nctalk/nctalk-notify.sh" > "$DRYRUN_BIN/nctalk.out" 2> "$DRYRUN_BIN/nctalk.err"
if [[ ! -s "$DRYRUN_BIN/slack.out" ]]; then
  pass "Slack dry-run leaves stdout empty"
else
  fail "Slack dry-run wrote stdout: $(cat "$DRYRUN_BIN/slack.out")"
fi
if grep -q 'Claude Code is done' "$DRYRUN_BIN/slack.err"; then
  pass "Slack dry-run reports the resolved message on stderr"
else
  fail "Slack dry-run did not report its message: $(cat "$DRYRUN_BIN/slack.err")"
fi
if [[ ! -s "$DRYRUN_BIN/slack.calls" ]]; then
  pass "Slack dry-run never invokes curl"
else
  fail "Slack dry-run invoked curl"
fi
if [[ ! -s "$DRYRUN_BIN/nctalk.out" ]]; then
  pass "Talk dry-run leaves stdout empty"
else
  fail "Talk dry-run wrote stdout: $(cat "$DRYRUN_BIN/nctalk.out")"
fi
if grep -q 'Claude Code is done' "$DRYRUN_BIN/nctalk.err"; then
  pass "Talk dry-run reports the resolved message on stderr"
else
  fail "Talk dry-run did not report its message: $(cat "$DRYRUN_BIN/nctalk.err")"
fi
if [[ ! -s "$DRYRUN_BIN/nctalk.calls" ]]; then
  pass "Talk dry-run never invokes curl"
else
  fail "Talk dry-run invoked curl"
fi

# ----------------------------------------------------------------------
# Recorded-stub tests (27-33): exercise the create/update/recreate/from-config
# CLI without ever touching a real sandbox. `sbx` is replaced by a recorded
# stub that logs every invocation, answers `ls -q` with the names a test wants
# it to "see", and captures `cp` sources so the tarballs it stages can be
# inspected on the host. The stub directory is put on PATH only for the calls
# that need it; the dry-run case proves that path is unnecessary.
#
# These drive the Python implementation directly: the modes and the config
# file do not exist in the retired bash script, and this section must run with
# SBX_IMPLS unset (the default).
# ----------------------------------------------------------------------
CREATE_SCRIPT="$REPO_DIR/docker-sbx-create-sandbox.py"
APPLY_SCRIPT="$REPO_DIR/docker-sbx-apply-profile.py"
RECORD_BIN="$TMPDIR/recordbin"
STUB_LOG="$TMPDIR/sbx-stub.log"
STUB_COPY="$TMPDIR/sbx-stub-copies"
mkdir -p "$RECORD_BIN" "$STUB_COPY"
cat > "$RECORD_BIN/sbx" << 'SCRIPT'
#!/bin/bash
if [[ -n "${SBX_STUB_LOG:-}" ]]; then
  printf '%s\n' "$*" >> "$SBX_STUB_LOG"
fi
# ls -q: report the sandbox names this test wants the implementation to see.
if [[ "${1:-}" == "ls" && "${2:-}" == "-q" ]]; then
  if [[ -n "${SBX_STUB_NAMES:-}" ]]; then
    printf '%s\n' "$SBX_STUB_NAMES"
  fi
  exit 0
fi
# cp <src> <name>:<dest>: keep the source so the suite can look inside it.
if [[ "${1:-}" == "cp" && $# -ge 3 && -n "${SBX_STUB_DIR:-}" ]]; then
  cp "$2" "$SBX_STUB_DIR/"
  exit 0
fi
# Everything else (create, exec, policy, rm, ...) is recorded and succeeds.
exit 0
SCRIPT
chmod +x "$RECORD_BIN/sbx"

reset_stub() {
  : > "$STUB_LOG"
  rm -rf "$STUB_COPY"
  mkdir -p "$STUB_COPY"
}
# Run the create script with the stub first on PATH. STUB_NAMES is what the
# stub reports for `sbx ls -q`; empty means "no sandbox of that name exists".
STUB_NAMES=""
run_stub_create() {
  env PATH="$RECORD_BIN:$PATH" \
    SBX_STUB_LOG="$STUB_LOG" SBX_STUB_DIR="$STUB_COPY" \
    SBX_STUB_NAMES="${STUB_NAMES:-}" \
    python3 "$CREATE_SCRIPT" "$@"
}
# Applying profiles is its own command now, not a flag on the create script.
run_stub_apply() {
  env PATH="$RECORD_BIN:$PATH" \
    SBX_STUB_LOG="$STUB_LOG" SBX_STUB_DIR="$STUB_COPY" \
    SBX_STUB_NAMES="${STUB_NAMES:-}" \
    python3 "$APPLY_SCRIPT" "$@"
}

# Fixtures shared by the new tests: a project directory and a profile
# directory outside the repository, so profiles/ is never modified.
NEW_PROJECT="$TMPDIR/new-project"
PROFILES="$TMPDIR/new-profiles"
mkdir -p "$NEW_PROJECT" \
  "$PROFILES/bundled/skills/demo" \
  "$PROFILES/bundled/project/commands" \
  "$PROFILES/node/skills/node"
printf 'name: bundled\ndescription: test bundle\npolicies:\n  network:\n    allow:\n      - pypi.org\n' \
  > "$PROFILES/bundled/profile.yaml"
printf 'demo skill\n' > "$PROFILES/bundled/skills/demo/SKILL.md"
printf '# demo command\n' > "$PROFILES/bundled/project/commands/demo.md"
printf 'name: node\npolicies:\n  network:\n    allow:\n      - registry.npmjs.org\n' \
  > "$PROFILES/node/profile.yaml"
printf 'node skill\n' > "$PROFILES/node/skills/node/SKILL.md"
# A flat policy profile, selected only with -profile-policy.
printf 'name: flat\npolicies:\n  network:\n    allow:\n      - example.com\n' \
  > "$PROFILES/flat.yaml"

# A directory profile that carries a sandbox kit. The kit directory is the
# build context for `sbx create --kit`; it must never be copied as a Claude
# asset.
mkdir -p "$PROFILES/flutter-sdk/kit"
printf 'name: flutter-sdk\ndescription: test kit profile\nkit: kit\npolicies:\n  network:\n    allow:\n      - pub.dev\n' \
  > "$PROFILES/flutter-sdk/profile.yaml"
printf '# syntax=docker/sandbox-kit:3\nschemaVersion: "3"\nkind: mixin\n' \
  > "$PROFILES/flutter-sdk/kit/flutter.yaml"
printf 'FROM scratch\n' > "$PROFILES/flutter-sdk/kit/flutter.dockerfile"

# ----------------------------------------------------------------------
# TEST 27: create with a bundled profile installs its assets and records it
# ----------------------------------------------------------------------
echo "TEST 27: create with a bundled profile installs its assets and records it"
BUNDLE_NAME="new-bundled"
CFG_DIR="$TMPDIR/cfg"
CFG_PATH="$CFG_DIR/$BUNDLE_NAME.yaml"
reset_stub
STUB_NAMES=""
CREATE_RC=0
CREATE_OUT="$(run_stub_create -n "$BUNDLE_NAME" -p "$NEW_PROJECT" -s false \
  -profile-directory bundled -profiles-dir "$PROFILES" --config-dir "$CFG_DIR" 2>&1)" || CREATE_RC=$?
if [[ "$CREATE_RC" == "0" ]]; then
  pass "Create returned success"
else
  fail "Create returned $CREATE_RC: $CREATE_OUT"
fi
if [[ -f "$CFG_PATH" ]] \
   && grep -q '^version: 1$' "$CFG_PATH" \
   && grep -qF "$BUNDLE_NAME" "$CFG_PATH" \
   && grep -q '^project:' "$CFG_PATH" \
   && grep -q '^profile_directories:$' "$CFG_PATH" \
   && grep -qF "'bundled'" "$CFG_PATH" \
   && grep -q '^profiles_dir:' "$CFG_PATH"; then
  pass "The saved config records version, name, project, directory profile and profiles dir"
else
  fail "Saved config is missing fields: $(cat "$CFG_PATH" 2>/dev/null || echo missing)"
fi
if printf '%s\n' "$CREATE_OUT" | grep -qF "Config saved: $CFG_PATH"; then
  pass "The config path is reported on stdout"
else
  fail "Config saved line missing: $CFG_PATH"
fi
if grep -qF "policy allow network --sandbox $BUNDLE_NAME" "$STUB_LOG"; then
  pass "The profile's network rule is applied to the sandbox"
else
  fail "No policy allow network call for $BUNDLE_NAME"
fi
if grep -qE "^cp .*/agent\.tgz $BUNDLE_NAME:" "$STUB_LOG" \
   && grep -qE "^cp .*/project\.tgz $BUNDLE_NAME:" "$STUB_LOG"; then
  pass "Both agent- and project-level asset tarballs are copied in"
else
  fail "A profile asset tarball was not copied: $(grep -E '^cp ' "$STUB_LOG" || echo none)"
fi
if grep -qE "^exec -i $BUNDLE_NAME sh -c .*tar -xzf " "$STUB_LOG"; then
  pass "Each tarball is extracted inside the sandbox with tar -xzf"
else
  fail "No tar -xzf exec for $BUNDLE_NAME"
fi
if tar -tzf "$STUB_COPY/agent.tgz" 2>/dev/null | grep -qx 'skills/demo/SKILL.md'; then
  pass "The agent tarball carries skills/demo/SKILL.md"
else
  fail "The agent tarball does not contain the bundled skill"
fi
if tar -tzf "$STUB_COPY/project.tgz" 2>/dev/null | grep -qx 'commands/demo.md'; then
  pass "The project tarball carries commands/demo.md"
else
  fail "The project tarball does not contain the bundled command"
fi

# ----------------------------------------------------------------------
# TEST 28: the config path is displayed at the very end of the run
# ----------------------------------------------------------------------
echo "TEST 28: the config path is displayed at the very end of the run"
# The summary ends with "Config saved: <path>" followed by the "Recreate
# later with:" hint (when a config was written), so the path must appear in
# that final block rather than scrolling past earlier in the output.
FINAL_BANNER_LINE="$(printf '%s\n' "$CREATE_OUT" | grep -n '^═' | tail -n 1 | cut -d: -f1 || true)"
CONFIG_SAVED_LINE="$(printf '%s\n' "$CREATE_OUT" | grep -nF "Config saved: $CFG_PATH" | tail -n 1 | cut -d: -f1 || true)"
if [[ -n "$FINAL_BANNER_LINE" && -n "$CONFIG_SAVED_LINE" && "$CONFIG_SAVED_LINE" -gt "$FINAL_BANNER_LINE" ]]; then
  pass "The config path is printed after the final sandbox banner"
else
  fail "Config saved line is not in the closing block (banner=$FINAL_BANNER_LINE, config=$CONFIG_SAVED_LINE)"
fi
LAST_TWO="$(printf '%s\n' "$CREATE_OUT" | grep -v '^[[:space:]]*$' | tail -n 2)"
if printf '%s\n' "$LAST_TWO" | grep -qF "Config saved: $CFG_PATH"; then
  pass "The config path is one of the last two non-empty lines"
else
  fail "The run did not end with the config path: $LAST_TWO"
fi

# ----------------------------------------------------------------------
# TEST 29: --dry-run writes no config and never invokes sbx
# ----------------------------------------------------------------------
echo "TEST 29: --dry-run writes no config and never invokes sbx"
DRY_CFG="$TMPDIR/cfg-dry"
EMPTY_BIN="$TMPDIR/emptybin"
mkdir -p "$EMPTY_BIN"
: > "$STUB_LOG"
PYTHON_BIN="$(command -v python3)"
# The stub is deliberately absent from PATH: a dry run needs no sbx at all.
DRY_RC=0
DRY_OUT="$(env PATH="$EMPTY_BIN" SBX_STUB_LOG="$STUB_LOG" "$PYTHON_BIN" "$CREATE_SCRIPT" \
  --dry-run -n new-dry -p "$NEW_PROJECT" -s false \
  -profile-directory bundled -profiles-dir "$PROFILES" --config-dir "$DRY_CFG" 2>&1)" || DRY_RC=$?
if [[ "$DRY_RC" == "0" ]]; then
  pass "Dry run returned success without sbx on PATH"
else
  fail "Dry run returned $DRY_RC: $DRY_OUT"
fi
if [[ ! -s "$STUB_LOG" ]]; then
  pass "No sbx command was actually executed"
else
  fail "Dry run invoked sbx: $(cat "$STUB_LOG")"
fi
if printf '%s\n' "$DRY_OUT" | grep -qF "Config would be saved: $DRY_CFG/new-dry.yaml"; then
  pass "Dry run reports the config it would write"
else
  fail "Config would be saved line missing"
fi
if printf '%s\n' "$DRY_OUT" | grep -qF '$ sbx create'; then
  pass "Dry run echoes the create command it would run"
else
  fail "Dry run did not echo the create command"
fi
DRY_FILES="$(find "$DRY_CFG" -type f 2>/dev/null || true)"
if [[ -z "$DRY_FILES" ]]; then
  pass "Dry run wrote no config file"
else
  fail "Dry run wrote: $DRY_FILES"
fi

# ----------------------------------------------------------------------
# TEST 30: docker-sbx-apply-profile.py applies and records a profile
# ----------------------------------------------------------------------
echo "TEST 30: docker-sbx-apply-profile.py applies and records a profile"
UPDATE_NAME="new-update"
UPDATE_CFG="$TMPDIR/cfg-update"
mkdir -p "$UPDATE_CFG"
cat > "$UPDATE_CFG/$UPDATE_NAME.yaml" << EOF
version: 1
name: '$UPDATE_NAME'
project: '$NEW_PROJECT'
scan_secrets: false
profiles:
  - 'python'
EOF
reset_stub
STUB_NAMES="$UPDATE_NAME"
UPDATE_RC=0
UPDATE_OUT="$(run_stub_apply -n "$UPDATE_NAME" -profile-directory node \
  -profiles-dir "$PROFILES" --config-dir "$UPDATE_CFG" 2>&1)" || UPDATE_RC=$?
if [[ "$UPDATE_RC" == "0" ]]; then
  pass "Profile update returned success for an existing sandbox"
else
  fail "Profile update returned $UPDATE_RC: $UPDATE_OUT"
fi
if ! grep -qE '^create( |$)' "$STUB_LOG"; then
  pass "Update mode never creates the sandbox again"
else
  fail "Update mode ran sbx create: $(cat "$STUB_LOG")"
fi
if grep -qF "policy allow network --sandbox $UPDATE_NAME" "$STUB_LOG" \
   && grep -qF 'tar -xzf' "$STUB_LOG"; then
  pass "The new profile's network rule and assets are applied"
else
  fail "The new profile was not fully applied: $(cat "$STUB_LOG")"
fi
if grep -qF "'python'" "$UPDATE_CFG/$UPDATE_NAME.yaml" \
   && grep -q '^profile_directories:$' "$UPDATE_CFG/$UPDATE_NAME.yaml" \
   && grep -qF "'node'" "$UPDATE_CFG/$UPDATE_NAME.yaml"; then
  pass "The config lists the policy and the new directory profile"
else
  fail "The config was not updated: $(cat "$UPDATE_CFG/$UPDATE_NAME.yaml" 2>/dev/null || echo missing)"
fi
if printf '%s\n' "$UPDATE_OUT" | grep -qF "Config updated: $UPDATE_CFG/$UPDATE_NAME.yaml"; then
  pass "The update is reported on stdout"
else
  fail "Config updated line missing"
fi
# The update flow is no longer reachable from the create script.
REJECT_RC=0
REJECT_OUT="$(run_stub_create -n "$UPDATE_NAME" -profile-directory node \
  -profiles-dir "$PROFILES" --apply-profile 2>&1)" || REJECT_RC=$?
if [[ "$REJECT_RC" != "0" ]] \
   && printf '%s\n' "$REJECT_OUT" | grep -qF "Unknown option: --apply-profile"; then
  pass "The create script rejects the retired --apply-profile flag"
else
  fail "The retired --apply-profile flag was not rejected: $REJECT_OUT"
fi

# ----------------------------------------------------------------------
# TEST 31: --recreate refuses without --force, and removes before creating
# ----------------------------------------------------------------------
echo "TEST 31: --recreate refuses without --force, and removes before creating"
RECREATE_NAME="new-recreate"
RECREATE_CFG="$TMPDIR/cfg-recreate"
mkdir -p "$RECREATE_CFG"
cat > "$RECREATE_CFG/$RECREATE_NAME.yaml" << EOF
version: 1
name: '$RECREATE_NAME'
project: '$NEW_PROJECT'
scan_secrets: false
EOF
reset_stub
STUB_NAMES="$RECREATE_NAME"
REFUSE_RC=0
REFUSE_OUT="$(run_stub_create -n "$RECREATE_NAME" --recreate \
  --config-dir "$RECREATE_CFG" 2>&1)" || REFUSE_RC=$?
if [[ "$REFUSE_RC" != "0" ]]; then
  pass "Recreate refuses when the sandbox already exists and --force is absent"
else
  fail "Recreate removed an existing sandbox without --force: $REFUSE_OUT"
fi
if ! grep -qE '^(create|rm)( |$)' "$STUB_LOG"; then
  pass "The refused run neither removed nor created anything"
else
  fail "The refused run touched the sandbox: $(cat "$STUB_LOG")"
fi
reset_stub
STUB_NAMES="$RECREATE_NAME"
FORCE_RC=0
FORCE_OUT="$(run_stub_create -n "$RECREATE_NAME" --recreate --force \
  --config-dir "$RECREATE_CFG" 2>&1)" || FORCE_RC=$?
if [[ "$FORCE_RC" == "0" ]]; then
  pass "Recreate with --force returned success"
else
  fail "Recreate with --force returned $FORCE_RC: $FORCE_OUT"
fi
RM_LINE="$(grep -m1 -nE "^rm $RECREATE_NAME$" "$STUB_LOG" | cut -d: -f1 || true)"
CREATE_LINE="$(grep -m1 -nE "^create --name $RECREATE_NAME( |$)" "$STUB_LOG" | cut -d: -f1 || true)"
if [[ -n "$RM_LINE" && -n "$CREATE_LINE" && "$RM_LINE" -lt "$CREATE_LINE" ]]; then
  pass "The existing sandbox is removed before the new one is created"
else
  fail "Expected rm before create (rm=$RM_LINE, create=$CREATE_LINE): $(cat "$STUB_LOG")"
fi
if [[ -f "$RECREATE_CFG/$RECREATE_NAME.yaml" ]]; then
  pass "A config still exists after the recreate"
else
  fail "The recreate lost the sandbox config"
fi

# ----------------------------------------------------------------------
# TEST 32: --from-config honours the saved volumes and records the import
# ----------------------------------------------------------------------
echo "TEST 32: --from-config honours the saved volumes and records the import"
IMPORT_NAME="new-imported"
IMPORT_CFG="$TMPDIR/cfg-import"
IMPORT_RO="$TMPDIR/import-ro"
IMPORT_RW="$TMPDIR/import-rw"
mkdir -p "$IMPORT_RO" "$IMPORT_RW"
IMPORT_FILE="$TMPDIR/imported.yaml"
cat > "$IMPORT_FILE" << EOF
version: 1
name: '$IMPORT_NAME'
project: '$NEW_PROJECT'
scan_secrets: false
profile_directories:
  - 'node'
profiles_dir: '$PROFILES'
volumes:
  - '$IMPORT_RO:ro'
  - '$IMPORT_RW:rw'
EOF
reset_stub
STUB_NAMES=""
IMPORT_RC=0
IMPORT_OUT="$(run_stub_create --from-config "$IMPORT_FILE" \
  --config-dir "$IMPORT_CFG" 2>&1)" || IMPORT_RC=$?
if [[ "$IMPORT_RC" == "0" ]]; then
  pass "Create-from-config returned success"
else
  fail "Create-from-config returned $IMPORT_RC: $IMPORT_OUT"
fi
IMPORT_CREATE_LINE="$(grep -m1 '^create ' "$STUB_LOG" || true)"
if printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF "create --name $IMPORT_NAME " \
   && printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF "$NEW_PROJECT"; then
  pass "The create command names the saved sandbox and project"
else
  fail "Unexpected create line: $IMPORT_CREATE_LINE"
fi
if printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF "$IMPORT_RO:ro" \
   && printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF " $IMPORT_RW" \
   && ! printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF "$IMPORT_RW:ro" \
   && ! printf '%s\n' "$IMPORT_CREATE_LINE" | grep -qF "$IMPORT_RW:rw"; then
  pass "The read-only volume is passed explicit and the read-write one bare"
else
  fail "Volume arguments are wrong: $IMPORT_CREATE_LINE"
fi
if [[ -f "$IMPORT_CFG/$IMPORT_NAME.yaml" ]] \
   && grep -q '^profile_directories:$' "$IMPORT_CFG/$IMPORT_NAME.yaml" \
   && grep -qF "'node'" "$IMPORT_CFG/$IMPORT_NAME.yaml"; then
  pass "The imported sandbox gets its own config under --config-dir"
else
  fail "The imported config was not written: $(cat "$IMPORT_CFG/$IMPORT_NAME.yaml" 2>/dev/null || echo missing)"
fi

# ----------------------------------------------------------------------
# TEST 33: an invalid profile bundle aborts before any config is written
# ----------------------------------------------------------------------
echo "TEST 33: an invalid profile bundle aborts before any config is written"
BAD_PROFILES="$TMPDIR/profiles-bad"
BAD_NAME="new-invalid"
BAD_CFG="$TMPDIR/cfg-bad"
mkdir -p "$BAD_PROFILES/invalid"
printf 'name: invalid\ndescription: bad bundle\n' > "$BAD_PROFILES/invalid/profile.yaml"
# settings.json at the bundle root would overwrite a file docker-sbx owns.
printf '{}\n' > "$BAD_PROFILES/invalid/settings.json"
reset_stub
STUB_NAMES=""
BAD_RC=0
BAD_OUT="$(run_stub_create -n "$BAD_NAME" -p "$NEW_PROJECT" -s false \
  -profile-directory invalid -profiles-dir "$BAD_PROFILES" --config-dir "$BAD_CFG" 2>&1)" || BAD_RC=$?
if [[ "$BAD_RC" != "0" ]]; then
  pass "Creation aborts on an invalid profile bundle"
else
  fail "Creation succeeded despite the invalid bundle: $BAD_OUT"
fi
if printf '%s\n' "$BAD_OUT" | grep -qF 'settings.json'; then
  pass "The error names the offending asset"
else
  fail "The error does not name settings.json: $BAD_OUT"
fi
if ! grep -qE '^create( |$)' "$STUB_LOG"; then
  pass "Nothing is created when the bundle is invalid"
else
  fail "The invalid bundle was detected only after sbx create: $(cat "$STUB_LOG")"
fi
if [[ ! -f "$BAD_CFG/$BAD_NAME.yaml" ]]; then
  pass "No config is written for a failed bundle"
else
  fail "A config was written despite the failure"
fi

# ----------------------------------------------------------------------
# TEST 34: a profile kit reaches sbx create, and the kit dir is not an asset
# ----------------------------------------------------------------------
echo "TEST 34: a profile kit reaches sbx create, and the kit dir is not an asset"
KIT_NAME="new-kit"
KIT_CFG="$TMPDIR/cfg-kit"
reset_stub
STUB_NAMES=""
KIT_RC=0
KIT_OUT="$(run_stub_create -n "$KIT_NAME" -p "$NEW_PROJECT" -s false \
  -profile-directory node -profile-directory flutter-sdk -profiles-dir "$PROFILES" \
  --config-dir "$KIT_CFG" 2>&1)" || KIT_RC=$?
if [[ "$KIT_RC" == "0" ]]; then
  pass "Create with a kit-bearing profile returned success"
else
  fail "Create with a kit-bearing profile returned $KIT_RC: $KIT_OUT"
fi
KIT_CREATE_LINE="$(grep -m1 '^create ' "$STUB_LOG" || true)"
if printf '%s\n' "$KIT_CREATE_LINE" | grep -qF -- "--kit $PROFILES/flutter-sdk/kit"; then
  pass "The kit directory is passed to sbx create --kit as an absolute path"
else
  fail "The kit was not passed to sbx create: $KIT_CREATE_LINE"
fi
if grep -qF "policy allow network --sandbox $KIT_NAME registry.npmjs.org" "$STUB_LOG" \
   && grep -qF "policy allow network --sandbox $KIT_NAME pub.dev" "$STUB_LOG"; then
  pass "Both repeated -profile-directory values contribute their network rules"
else
  fail "The repeated profile rules were not all applied: $(cat "$STUB_LOG")"
fi
if [[ -f "$KIT_CFG/$KIT_NAME.yaml" ]] \
   && grep -q '^profile_directories:$' "$KIT_CFG/$KIT_NAME.yaml" \
   && grep -qF "'node'" "$KIT_CFG/$KIT_NAME.yaml" \
   && grep -qF "'flutter-sdk'" "$KIT_CFG/$KIT_NAME.yaml"; then
  pass "The saved config records both directory profiles, including the kit profile"
else
  fail "The saved config is missing a profile: $(cat "$KIT_CFG/$KIT_NAME.yaml" 2>/dev/null || echo missing)"
fi
if tar -tzf "$STUB_COPY/agent.tgz" 2>/dev/null | grep -q '^skills/node/SKILL.md$' \
   && ! tar -tzf "$STUB_COPY/agent.tgz" 2>/dev/null | grep -q '^kit/'; then
  pass "The Claude asset tarball carries the skill but not the kit build context"
else
  fail "Unexpected asset tarball contents: $(tar -tzf "$STUB_COPY/agent.tgz" 2>/dev/null || echo missing)"
fi

# ----------------------------------------------------------------------
# TEST 35: apply-profile refuses a kit-bearing profile
# ----------------------------------------------------------------------
echo "TEST 35: apply-profile refuses a kit-bearing profile"
KIT_UPDATE_NAME="existing-kit"
KIT_UPDATE_CFG="$TMPDIR/cfg-kit-update"
mkdir -p "$KIT_UPDATE_CFG"
cat > "$KIT_UPDATE_CFG/$KIT_UPDATE_NAME.yaml" << EOF
version: 1
name: '$KIT_UPDATE_NAME'
project: '$NEW_PROJECT'
scan_secrets: false
EOF
reset_stub
STUB_NAMES="$KIT_UPDATE_NAME"
KIT_UP_RC=0
KIT_UP_OUT="$(run_stub_apply -n "$KIT_UPDATE_NAME" -profile-directory flutter-sdk \
  -profiles-dir "$PROFILES" --config-dir "$KIT_UPDATE_CFG" 2>&1)" || KIT_UP_RC=$?
if [[ "$KIT_UP_RC" != "0" ]] \
   && printf '%s\n' "$KIT_UP_OUT" | grep -qF "includes a sandbox kit"; then
  pass "The update command refuses a kit-bearing profile"
else
  fail "The update command did not refuse the kit profile: $KIT_UP_OUT"
fi
if ! grep -qE '^(create|exec|cp|policy)( |$)' "$STUB_LOG"; then
  pass "Nothing is created or touched when the profile carries a kit"
else
  fail "The refused update touched the sandbox: $(cat "$STUB_LOG")"
fi

# ----------------------------------------------------------------------
# TEST 36: -profile-policy and -profile-directory are strict about kind
# ----------------------------------------------------------------------
echo "TEST 36: -profile-policy and -profile-directory are strict about kind"
POLICY_KIND_RC=0
POLICY_KIND_OUT="$(run_stub_create -n new-kind -p "$NEW_PROJECT" -s false \
  --dry-run -profile-policy bundled -profiles-dir "$PROFILES" \
  --config-dir "$TMPDIR/cfg-kind" 2>&1)" || POLICY_KIND_RC=$?
if [[ "$POLICY_KIND_RC" != "0" ]] \
   && printf '%s\n' "$POLICY_KIND_OUT" | grep -qF \
      "is a directory profile; select it with -profile-directory bundled"; then
  pass "-profile-policy refuses a directory profile and names the right flag"
else
  fail "-profile-policy accepted or misreported a directory profile: $POLICY_KIND_OUT"
fi
DIRECTORY_KIND_RC=0
DIRECTORY_KIND_OUT="$(run_stub_create -n new-kind -p "$NEW_PROJECT" -s false \
  --dry-run -profile-directory flat -profiles-dir "$PROFILES" \
  --config-dir "$TMPDIR/cfg-kind" 2>&1)" || DIRECTORY_KIND_RC=$?
if [[ "$DIRECTORY_KIND_RC" != "0" ]] \
   && printf '%s\n' "$DIRECTORY_KIND_OUT" | grep -qF \
      "is a flat policy profile; select it with -profile-policy flat"; then
  pass "-profile-directory refuses a flat policy profile and names the right flag"
else
  fail "-profile-directory accepted or misreported a flat profile: $DIRECTORY_KIND_OUT"
fi
FLAT_RC=0
FLAT_OUT="$(run_stub_create -n new-kind -p "$NEW_PROJECT" -s false \
  --dry-run -profile-policy flat -profiles-dir "$PROFILES" \
  --config-dir "$TMPDIR/cfg-kind" 2>&1)" || FLAT_RC=$?
if [[ "$FLAT_RC" == "0" ]] && printf '%s\n' "$FLAT_OUT" | grep -qF "allowing example.com"; then
  pass "-profile-policy accepts a flat policy profile"
else
  fail "-profile-policy did not apply the flat policy: $FLAT_OUT"
fi

# ----------------------------------------------------------------------
# TEST 37: the protected-path guard blocks the tools and Bash
# ----------------------------------------------------------------------
echo "TEST 37: the protected-path guard blocks the tools and Bash"
GUARD="$SCRIPTS_DIR/sandbox/docker-sbx-guard-protected-paths.sh"
guard_check() {
  local label="$1" expected="$2" payload="$3"
  local rc=0
  printf '%s' "$payload" | bash "$GUARD" >/dev/null 2>"$TMPDIR/guard.err" || rc=$?
  if [[ "$rc" == "$expected" ]]; then
    pass "$label"
  else
    fail "$label (expected rc $expected, got $rc: $(cat "$TMPDIR/guard.err"))"
  fi
}
guard_check "Bash naming google-services.json is blocked" 2 \
  '{"tool_name":"Bash","tool_input":{"command":"cat google-services.json"}}'
guard_check "Quoted or escaped names are still blocked" 2 \
  '{"tool_name":"Bash","tool_input":{"command":"cat google\\-services.json"}}'
guard_check "Read of GoogleService-Info.plist is blocked" 2 \
  '{"tool_name":"Read","tool_input":{"file_path":"/c/proj/GoogleService-Info.plist"}}'
guard_check "Write of launch.json is blocked" 2 \
  '{"tool_name":"Write","tool_input":{"file_path":".vscode/launch.json"}}'
guard_check "Grep aimed at a protected file is blocked" 2 \
  '{"tool_name":"Grep","tool_input":{"path":"/c/proj/google-services.json","pattern":"x"}}'
guard_check "An unrelated Bash command is allowed" 0 \
  '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}'
guard_check "An unrelated Read is allowed" 0 \
  '{"tool_name":"Read","tool_input":{"file_path":"/c/proj/README.md"}}'
if grep -qF "protected by docker-sbx" "$TMPDIR/guard.err" 2>/dev/null; then
  pass "The block message names the protection"
else
  # The last check was allowed; run one block again for the message.
  printf '%s' '{"tool_name":"Bash","tool_input":{"command":"cat launch.json"}}' \
    | bash "$GUARD" >/dev/null 2>"$TMPDIR/guard.err" || true
  if grep -qF "protected by docker-sbx" "$TMPDIR/guard.err"; then
    pass "The block message names the protection"
  else
    fail "The block message is missing: $(cat "$TMPDIR/guard.err")"
  fi
fi

# ----------------------------------------------------------------------
# TEST 38: protected-path hook installation is idempotent and removable
# ----------------------------------------------------------------------
echo "TEST 38: protected-path hook installation is idempotent and removable"
PG_HOME="$TMPDIR/protect-home"
PG_SETTINGS="$PG_HOME/.claude/settings.json"
mkdir -p "$PG_HOME/.claude"
printf '{}\n' > "$PG_SETTINGS"
stage_guard() { cp "$GUARD" "$PG_HOME/guard-protected-paths.sh"; }
install_guard() {
  stage_guard
  HOME="$PG_HOME" bash -c \
    "source '$INSTALLER_FNS'; protectedpaths=true; install_protected_paths_hook" \
    >/dev/null 2>&1 || true
}
install_guard
if [[ -x "$PG_HOME/.claude/hooks/guard-protected-paths.sh" ]]; then
  pass "The guard hook is installed and executable"
else
  fail "The guard hook was not installed"
fi
if [[ "$(jq -r '[.hooks.PreToolUse[]? | .hooks[]? | select(.command|endswith("guard-protected-paths.sh"))] | length' "$PG_SETTINGS" 2>/dev/null || echo err)" == "1" ]]; then
  pass "settings.json has one PreToolUse entry for the guard"
else
  fail "Unexpected PreToolUse entries: $(cat "$PG_SETTINGS" 2>/dev/null || echo missing)"
fi
if [[ "$(jq -r '[.hooks.PreToolUse[]? | select(.matcher|test("Bash")) | .matcher] | length' "$PG_SETTINGS" 2>/dev/null || echo err)" == "1" ]]; then
  pass "The entry matches the Bash and file tools"
else
  fail "The guard matcher is missing: $(cat "$PG_SETTINGS" 2>/dev/null || echo missing)"
fi
install_guard
if [[ "$(jq -r '[.hooks.PreToolUse[]? | .hooks[]? | select(.command|endswith("guard-protected-paths.sh"))] | length' "$PG_SETTINGS" 2>/dev/null || echo err)" == "1" ]]; then
  pass "Re-running setup does not duplicate the guard entry"
else
  fail "The guard entry was duplicated or lost: $(cat "$PG_SETTINGS" 2>/dev/null || echo missing)"
fi
HOME="$PG_HOME" bash -c \
  "source '$INSTALLER_FNS'; protectedpaths=false; install_protected_paths_hook" \
  >/dev/null 2>&1 || true
if [[ ! -e "$PG_HOME/.claude/hooks/guard-protected-paths.sh" ]] \
   && [[ "$(jq -r '[.hooks.PreToolUse[]? | .hooks[]? | select(.command|endswith("guard-protected-paths.sh"))] | length' "$PG_SETTINGS" 2>/dev/null || echo err)" == "0" ]]; then
  pass "protectedpaths=false removes the hook and its settings entry"
else
  fail "Disabling left the guard behind: $(cat "$PG_SETTINGS" 2>/dev/null || echo missing)"
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
