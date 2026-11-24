#!/usr/bin/env bash
#
# PreToolUse guard for docker-sbx sandboxes.
#
# Claude Code runs this before Bash, Read, Write, Edit, MultiEdit, NotebookEdit
# and Grep calls. It blocks a call whose command or path names one of the
# protected files, so neither the tools nor a shell command can read or write
# them without the user's help.
#
# Contract:
#   stdin  - the hook payload as JSON
#   exit 0 - no objection; the call continues through the normal permission flow
#   exit 2 - block the call; stderr is shown to the model as the reason
#
# This is a text match on the tool input, not a sandbox. It stops accidental
# access and obvious commands, not deliberate obfuscation (a variable, a helper
# script, a glob). A hard boundary would need OS-level permissions or keeping
# the files outside the mounted volumes.
#
# jq is required. It is installed by docker-sbx-setup-sandbox.sh before this
# hook is wired; if it is missing the payload cannot be inspected, so the hook
# stays out of the way instead of blocking every call.

set -uo pipefail

payload="$(cat)"

tool="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
[[ -n "$tool" ]] || exit 0

case "$tool" in
  Bash)
    text="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
    ;;
  Read|Write|Edit|MultiEdit)
    text="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)" || exit 0
    ;;
  NotebookEdit)
    text="$(printf '%s' "$payload" | jq -r '.tool_input.notebook_path // empty' 2>/dev/null)" || exit 0
    ;;
  Grep)
    # A path or glob that names the file. A recursive grep over the project
    # root cannot be caught by a text match; it is documented as best-effort.
    text="$(printf '%s' "$payload" | jq -r '[.tool_input.path // "", .tool_input.glob // ""] | join(" ")' 2>/dev/null)" || exit 0
    ;;
  *)
    exit 0
    ;;
esac

[[ -n "$text" ]] || exit 0

# Drop shell quoting/escaping so that "google-services".json and
# google\-services.json still match. Globs, variables, and indirection
# (g*services.json, $FILE, a helper script) remain out of scope.
text="${text//\\/}"
text="${text//\"/}"
text="${text//\'/}"

# Case-insensitive: the sandbox filesystem may be case-insensitive, and quoting
# or escaping in the command must not defeat a literal substring match.
shopt -s nocasematch

protected=(
  'GoogleService-Info.plist'
  'google-services.json'
  'launch.json'
)

for needle in "${protected[@]}"; do
  if [[ "$text" == *"$needle"* ]]; then
    printf 'Blocked: %s is protected by docker-sbx. Read, write or shell access is not allowed; ask the user to handle this file outside the sandbox.\n' "$needle" >&2
    exit 2
  fi
done

exit 0
