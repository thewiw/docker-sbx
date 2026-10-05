#!/usr/bin/env bash
#
# docker-sbx-update-sandboxes.sh
#
# Refresh packages inside sbx (Docker Sandbox) environments:
#     sudo apt update  ->  sudo apt upgrade -y  ->  sudo apt autoremove -y
#
# Usage:
#   ./docker-sbx-update-sandboxes.sh                          # every sandbox
#   ./docker-sbx-update-sandboxes.sh -i bc,web-1              # only these
#   ./docker-sbx-update-sandboxes.sh -x bc,web-1              # all except these
#   ./docker-sbx-update-sandboxes.sh -i bc,web-1 -x bc        # bc filtered out
#   ./docker-sbx-update-sandboxes.sh --help
#
# Recommended invocation (sbx wants a real TTY):
#   script -q /dev/null ./docker-sbx-update-sandboxes.sh [OPTIONS]
#
# !! Never write the chain unquoted on your shell command line:
#      sbx exec -ti box apt update && apt upgrade -y && apt autoremove
#    runs only the first command in the sandbox; the other two run ON THIS HOST.
#
set -uo pipefail

### 0. Usage --------------------------------------------------------------
usage() {
  echo "Usage: $(basename "$0") [--help] [-i LIST] [-x LIST]"
  cat <<'EOF'

With no -i, every sandbox returned by 'sbx ls -q' is refreshed.
With -i, only the listed sandboxes are processed and 'sbx ls' is never
called (useful when listing is slow or unreliable).

  -i, --include LIST   comma- or space-separated sandbox names to UPDATE.
                       Repeatable. Merged with the INCLUDE env var.
                       Omit it to mean "all sandboxes".
  -x, --exclude LIST   comma- or space-separated sandbox names to SKIP.
                       Repeatable. Merged with the SKIP_LIST env var.
                       Exclusions win over inclusions.

Environment options:
  CLOUD=1                target cloud sandboxes (adds global --cloud)
  DRY_RUN=1              print what would run, change nothing
  INCLUDE="a b"          same as -i, for env-based configuration
  SKIP_LIST="a b"        same as -x, for env-based configuration
  SBX_TIMEOUT=120        seconds allowed per query call
  SBX_EXEC_FLAGS=-ti     force exec flags instead of auto-detection
  SBX_LS_CMD='...'       custom listing command (names on stdout)
  APT_CHAIN='...'        custom in-sandbox command chain
  APT=apt-get|apt        package manager
  SUDO='sudo'            or 'sudo -n' to never prompt (fails fast instead)
EOF
}

### 1. Command line -------------------------------------------------------
INCLUDE_RAW="${INCLUDE:-}"
EXCLUDE_RAW="${SKIP_LIST:-}"

while [ $# -gt 0 ]; do
  arg="$1"; shift
  case "$arg" in
    -h|--help)      usage; exit 0 ;;
    --)             continue ;;
    --include=*)    INCLUDE_RAW="$INCLUDE_RAW ${arg#--include=}" ;;
    --include|-i)
                    if [ $# -eq 0 ]; then
                      echo "missing sandbox list after $arg" >&2; exit 2
                    fi
                    INCLUDE_RAW="$INCLUDE_RAW $1"; shift ;;
    --exclude=*)    EXCLUDE_RAW="$EXCLUDE_RAW ${arg#--exclude=}" ;;
    --exclude|-x)
                    if [ $# -eq 0 ]; then
                      echo "missing sandbox list after $arg" >&2; exit 2
                    fi
                    EXCLUDE_RAW="$EXCLUDE_RAW $1"; shift ;;
    -*)             echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    *)              echo "unexpected argument: $arg (use -i/--include to name sandboxes)" >&2
                    usage >&2; exit 2 ;;
  esac
done

# Normalise both lists once: commas and runs of whitespace become single
# spaces, padded with spaces at both ends so " $name " membership tests work.
INCLUDE=" $(printf '%s' "$INCLUDE_RAW" | tr ',' ' ' | tr -s '[:space:]' ' ') "
EXCLUDE=" $(printf '%s' "$EXCLUDE_RAW" | tr ',' ' ' | tr -s '[:space:]' ' ') "

### 2. Configuration ------------------------------------------------------
DRY_RUN="${DRY_RUN:-0}"
CLOUD="${CLOUD:-0}"
APT="${APT:-apt-get}"               # script-friendly; 'apt' also works
SUDO="${SUDO:-sudo}"                # sandbox agents are unprivileged; apt needs root
SBX_TIMEOUT="${SBX_TIMEOUT:-120}"   # queries only
SBX_LS_CMD="${SBX_LS_CMD:-}"
SBX_EXEC_FLAGS="${SBX_EXEC_FLAGS:-}"

# Executed inside each sandbox as a single shell command string.
# Each apt command gets its own $SUDO prefix. The env assignment must come
# AFTER sudo (sudo parses it for the command); 'DEBIAN_FRONTEND=... sudo apt'
# would have the variable stripped by sudo's env_reset.
APT_CHAIN="${APT_CHAIN:-\
echo '==> sudo apt update'        && $SUDO DEBIAN_FRONTEND=noninteractive $APT update         && \
echo '==> sudo apt upgrade'       && $SUDO DEBIAN_FRONTEND=noninteractive $APT -y upgrade     && \
echo '==> sudo apt autoremove'    && $SUDO DEBIAN_FRONTEND=noninteractive $APT -y autoremove}"
### -----------------------------------------------------------------------

SBX=(sbx)
[ "$CLOUD" = "1" ] && SBX=(sbx --cloud)

# Portable timeout, for query calls only. macOS: GNU coreutils ships gtimeout.
if   command -v timeout  >/dev/null 2>&1; then TO=(timeout "$SBX_TIMEOUT")
elif command -v gtimeout >/dev/null 2>&1; then TO=(gtimeout "$SBX_TIMEOUT")
else TO=(); echo "warning: no timeout/gtimeout found" >&2
fi

# -t needs a terminal; -i alone is the safe non-interactive default.
if [ -z "$SBX_EXEC_FLAGS" ]; then
  if [ -t 0 ] && [ -t 1 ]; then SBX_EXEC_FLAGS="-ti"; else SBX_EXEC_FLAGS="-i"; fi
fi

# Queries: non-interactive, stdin closed, time-limited.
sbx_run()  { "${TO[@]}" "${SBX[@]}" "$@" </dev/null; }

# Exec: no timeout (a TTY-attached exec in a background process group gets
# stopped by SIGTTIN, which looks exactly like a hang). stdin stays inherited
# so a sudo password prompt is answerable when SUDO='sudo'.
sbx_exec() { local name="$1" cmd="$2"
             "${SBX[@]}" exec $SBX_EXEC_FLAGS "$name" sh -c "$cmd"; }

### 3. Enumerate sandboxes ------------------------------------------------
list_sandboxes() {
  local out; out="$(mktemp)"

  if [ -n "$SBX_LS_CMD" ]; then
    if sh -c "$SBX_LS_CMD" >"$out"; then
      sed -n 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d; p' "$out" | sort -u
      rm -f "$out"; return 0
    fi
  fi

  # Documented "names only" output.
  if sbx_run ls -q >"$out"; then
    sed -n 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d; p' "$out" | sort -u
    rm -f "$out"; return 0
  fi

  # Fallback: JSON output.
  if sbx_run ls --json >"$out" && grep -q '[{[]' "$out"; then
    if command -v jq >/dev/null 2>&1; then
      jq -r '.. | objects | (.name // .Name // empty)' "$out" | grep -v '^$' | sort -u
    else
      grep -oE '"(name|Name)"[[:space:]]*:[[:space:]]*"[^"]+"' "$out" |
        sed -E 's/.*"([^"]+)"$/\1/' | sort -u
    fi
    rm -f "$out"; return 0
  fi

  # Last resort: first column of the human-readable table.
  sbx_run ls >"$out" || true
  awk 'NF && $1 !~ /^(NAME|ID|SANDBOX|AGENT)$/ && $1 !~ /^[-─=]+$/ { print $1 }' "$out" | sort -u
  rm -f "$out"
}

echo "Preflight (exec flags: $SBX_EXEC_FLAGS, sudo prefix: '$SUDO')"
sbx_run version
sbx_run daemon status

if [ -n "$(printf '%s' "$INCLUDE" | tr -d ' ')" ]; then
  # Explicit -i list: no sbx ls, no preflight listing.
  read -r -a sandboxes <<<"$INCLUDE"
  echo "Including ${#sandboxes[@]} sandbox(es): ${sandboxes[*]}"
else
  # No -i: list everything.
  LIST_FILE="$(mktemp)"
  if ! list_sandboxes >"$LIST_FILE"; then
    echo "could not list sandboxes (check: sbx daemon status && sbx login)" >&2
    rm -f "$LIST_FILE"; exit 2
  fi
  mapfile -t sandboxes <"$LIST_FILE"
  rm -f "$LIST_FILE"

  if [ "${#sandboxes[@]}" -eq 0 ]; then
    echo "no sandboxes found" >&2
    exit 0
  fi
  echo "Found ${#sandboxes[@]} sandbox(es): ${sandboxes[*]}"
fi

if [ -n "$(printf '%s' "$EXCLUDE" | tr -d ' ')" ]; then
  echo "Excluding:${EXCLUDE% }"
fi

# Warn about names present in both lists (exclusion wins).
for name in $INCLUDE; do
  case "$EXCLUDE" in
    *" $name "*) echo "note: $name is both included and excluded - it will be skipped" ;;
  esac
done

### 4. Run the sudo apt chain in each sandbox -----------------------------
ok=0; failed=0; skipped=0

for name in "${sandboxes[@]}"; do
  case "$EXCLUDE" in
    *" $name "*) echo "skip $name (excluded)"; skipped=$((skipped+1)); continue ;;
  esac

  echo "== $name =="

  if [ "$DRY_RUN" = "1" ]; then
    echo "   would run: ${SBX[*]} exec $SBX_EXEC_FLAGS $name sh -c"
    echo "   $APT_CHAIN"
    continue
  fi

  if sbx_exec "$name" "$APT_CHAIN"; then
    ok=$((ok+1))
  else
    echo "   FAILED (rc=$?)" >&2
    failed=$((failed+1))
  fi
done

### 5. Summary ------------------------------------------------------------
echo "Done. ok=$ok failed=$failed skipped=$skipped"
[ "$failed" -eq 0 ]
