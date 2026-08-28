#!/bin/bash

set -Eeuo pipefail   # Safer Bash: fail on errors, unset vars, pipeline failures

# ----------------------------------------------------------------------
# Default values --------------------------------------------------------
# ----------------------------------------------------------------------
name=""
path=""
envfile=""
secrets="true"
secretsfile=""
gensecretsfile=""
volumes=()
volume_args=()
profiles=""
profile_directory=""

usage() {
  scriptname=`basename "$0"`
  echo "Usage: $scriptname -n [sandbox name] -p [project path] -e [sandbox env file] -s [check secrets] -sf [secrets config file]"
  echo "Options:"
  echo "  -n string     Define the name of the new sandbox [mandatory]"
  echo "  -p string     Absolute path of project's directory [mandatory]"
  echo "  -e string     Env file [optional]"
  echo "  -s true/false Check for secrets (default true)"
  echo "  -sf string    Secrets config JSON file [optional]"
  echo "  -gsf string   Generate a default secrets config JSON file and exit"
  echo "  -v path       Mount an extra host directory/file into the sandbox (absolute path)."
  echo "                Repeat -v for multiple mounts. Read-only by default; append :rw to"
  echo "                mount read-write (or :ro to be explicit). e.g. -v /mnt/c/docs -v /mnt/c/data:rw"
  echo "  -profile names Comma-separated list of policy profiles to apply (order matters) [optional]"
  echo "  -profile-directory path Directory containing profile YAML files [optional, default: ./profiles]"
  echo ""
  echo "  -h, --help Show this help and exit"
  echo ""
  echo "Example(s):"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -e sbx.env"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -e sbx.env -sf secrets.json"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -v /mnt/c/docs -v /mnt/c/data:rw"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -profile python"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -profile python,java"
  echo "  from WSL : $scriptname -gsf secrets.json"
  echo ""
  exit 1
}

ensure_pyyaml() {
  if python3 -c "import yaml" 2>/dev/null; then
    return 0
  fi

  echo "Error: PyYAML is required for profile parsing but is not installed."
  echo "Install it with: sudo apt-get install python3-yaml"
  exit 1
}

generate_secrets_file() {
  local filepath="$gensecretsfile"
  cat > "$filepath" << 'EOF'
{
  "search": [
    "google-services.json",
    "key.properties",
    "*.jks",
    "*.cert",
    "*.crt",
    "*.key"
  ],
  "avoid": [
    ".venv",
    "node_modules",
    "bower_components"
  ]
}
EOF
  echo "Generated default secrets config file: $filepath"
}

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
    echo ""
    return 0
  else
    echo "Looking for secrets files in $path, please wait..."
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

  #echo "find args = ${find_args[@]}"
  secrets_found=`find "${find_args[@]}"`

  if echo "$secrets_found" 2> /dev/null | grep -q .; then
    echo "$secrets_found"
    echo "Error: secrets files detected within this project, make sure those can not be reached from sandbox and run this command again"
    echo ""
    exit 1
  else
    echo "No secrets files detected within this project, let's continue"
    echo ""
  fi
}

check_project_secrets_git() {
  gitrepo=""
  if [ -d "$path/.git" ]; then
    gitrepo="$path/.git"
  elif gitdir=$(find "$path" -type d -name .git -print -quit 2>/dev/null); then
    gitrepo="$gitdir"
  else
    gitrepo="$path"
  fi

  if [ -n "$gitrepo" ]; then
    echo "Detecting secrets within GIT repository ${gitrepo}, please wait..."
    if ! docker run --rm -v "$path":/project -v "$gitrepo":/repo -v "$(pwd)":/report ghcr.io/gitleaks/gitleaks:latest git /repo --gitleaks-ignore-path /project --report-path "/report/${name}_gitleaks.json"; then
      echo "Error: detected at least 1 leak in git repository ${gitrepo}, fix it/them before running sandbox"
      echo ""
      exit 1
    else
      echo ""
    fi
  else
    echo "GIT repository not found, not running leaks detection"
    echo ""
  fi
}

check_project_secrets() {
  if [[ "${secrets,,}" != "false" ]]; then
    echo ""
    echo "════════════════════════"
    echo "Checking project secrets $name"
    echo "════════════════════════"
    echo ""

    check_project_secrets_files
    check_project_secrets_git
  else
    echo ""
    echo "════════════════════════════════════════════════════════════════════════════════════════"
    echo "Not checking project secrets, MAKE SURE NO SECRET CAN BE LEAKED THROUGH THIS PROJECT !!!"
    echo "════════════════════════════════════════════════════════════════════════════════════════"
    echo ""
  fi
}

build_volume_args() {
  volume_args=()
  if [[ ${#volumes[@]} -eq 0 ]]; then
    return 0
  fi

  local spec hostpath mode low
  for spec in "${volumes[@]}"; do
    # An optional trailing :ro / :rw suffix selects the mount mode.
    # Without a suffix the volume is mounted read-only (the default).
    low="${spec,,}"
    mode="ro"
    hostpath="$spec"
    if [[ "$low" == *:ro ]]; then
      mode="ro"
      hostpath="${spec:0:-3}"
    elif [[ "$low" == *:rw ]]; then
      mode="rw"
      hostpath="${spec:0:-3}"
    fi

    if [[ "$hostpath" != /* ]]; then
      echo "Error: volume path '$hostpath' must be an absolute path"
      exit 1
    fi
    if [[ ! -e "$hostpath" ]]; then
      echo "Error: volume path '$hostpath' does not exist"
      exit 1
    fi

    if [[ "$mode" == "ro" ]]; then
      volume_args+=("${hostpath}:ro")
    else
      volume_args+=("$hostpath")
    fi
    echo "Mounting $hostpath (read-$([[ $mode == ro ]] && echo only || echo write)) into sandbox $name"
  done
  echo ""
}

create_sandbox() {
  prjname=`basename "$path"`

  echo ""
  echo "════════════════"
  echo "Creating sandbox $name"
  echo "════════════════"
  echo ""
  if [[ ${#volume_args[@]} -gt 0 ]]; then
    sbx create --name "$name" claude "$path" "${volume_args[@]}"
  else
    sbx create --name "$name" claude "$path"
  fi
}

setup_sandbox() {
  sbx_home="/home/agent"

  echo ""
  echo "════════════════"
  echo "Updating sandbox $name"
  echo "════════════════"
  echo ""

  sbx cp ./docker-sbx-setup-sandbox.sh "$name":"$sbx_home/setup-sandbox.sh"
  sbx exec -ti "$name" sudo chmod 755 "$sbx_home/setup-sandbox.sh"
  if [[ -z "$envfile" ]]; then
    sbx exec -ti "$name" bash "$sbx_home/setup-sandbox.sh" "$path"
  else
    echo "Using env file $envfile"
    envbasename="$(basename "$envfile")"
    sbx cp "$envfile" "$name":"$sbx_home/$envbasename"
    sbx exec -ti "$name" sudo chmod 644 "$sbx_home/$envbasename"
    sbx exec -ti "$name" bash "$sbx_home/setup-sandbox.sh" "$path" "$sbx_home/$envbasename"
  fi
}

update_sbx_policy() {
  if [[ -z "$envfile" || ! -f "$envfile" ]]; then
    return 0
  fi

  local base_url=""
  local api_key=""

  if grep -q '^ANTHROPIC_BASE_URL=' "$envfile" 2>/dev/null; then
    base_url=$(grep '^ANTHROPIC_BASE_URL=' "$envfile" | cut -d '=' -f 2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
  fi

  if grep -q '^ANTHROPIC_API_KEY=' "$envfile" 2>/dev/null; then
    api_key=$(grep '^ANTHROPIC_API_KEY=' "$envfile" | cut -d '=' -f 2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
  fi

  if [[ -n "$base_url" ]]; then # LLM server hostname is provided
    local allowed_host
    allowed_host=$(echo "$base_url" | sed -E 's|https?://||; s|[:/].*||')
    echo ""
    echo "Allowing $allowed_host in sandbox $name network policy"
    sbx policy allow network --sandbox "$name" "$allowed_host"
  elif [[ -n "$api_key" ]]; then # Using Anthropic API key
    echo ""
    echo "Allowing api.anthropic.com in sandbox $name network policy"
    sbx policy allow network --sandbox "$name" "api.anthropic.com"
  else # Using Anthropic regular login method
    echo ""
    echo "Allowing claude.ai and platform.claude.com in sandbox $name network policy"
    sbx policy allow network --sandbox "$name" "claude.ai"
    sbx policy allow network --sandbox "$name" "platform.claude.com"
  fi
}

trim() {
  local var="$*"
  var="${var#"${var%%[![:space:]]*}"}"
  var="${var%"${var##*[![:space:]]}"}"
  printf '%s' "$var"
}

parse_profile_yaml() {
  local profile_path="$1"
  python3 - "$profile_path" <<'PYEOF'
import sys, yaml, json

profile_path = sys.argv[1]

try:
    with open(profile_path, 'r') as f:
        data = yaml.safe_load(f)
except Exception as e:
    print(f"Error: failed to parse profile {profile_path}: {e}", file=sys.stderr)
    sys.exit(1)

if data is None:
    data = {}

if not isinstance(data, dict):
    print(f"Error: profile {profile_path} must be a YAML mapping", file=sys.stderr)
    sys.exit(1)

for key in data.keys():
    lowered = key.lower()
    if lowered in ('global', 'default', 'all_sandboxes', 'sandbox_default'):
        print(f"Error: profile {profile_path} contains forbidden global key '{key}'", file=sys.stderr)
        sys.exit(1)

allowed = []
denied = []

policies = data.get('policies', {})
if policies is None:
    policies = {}
if not isinstance(policies, dict):
    print(f"Error: profile {profile_path} 'policies' must be a mapping", file=sys.stderr)
    sys.exit(1)

for section in policies.keys():
    if section.lower() != 'network':
        print(f"Error: profile {profile_path} has unsupported policy section '{section}' (only 'network' is supported)", file=sys.stderr)
        sys.exit(1)

network = policies.get('network', {})
if network is None:
    network = {}
if not isinstance(network, dict):
    print(f"Error: profile {profile_path} 'policies.network' must be a mapping", file=sys.stderr)
    sys.exit(1)

for action in ('allow', 'deny'):
    entries = network.get(action, [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        print(f"Error: profile {profile_path} 'policies.network.{action}' must be a list", file=sys.stderr)
        sys.exit(1)
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            print(f"Error: profile {profile_path} 'policies.network.{action}' contains invalid entry", file=sys.stderr)
            sys.exit(1)
        if action == 'allow':
            allowed.append(entry.strip())
        else:
            denied.append(entry.strip())

print(json.dumps({'allow': allowed, 'deny': denied}))
PYEOF
}

apply_profiles() {
  if [[ -z "$profiles" ]]; then
    return 0
  fi

  local profiles_dir
  if [[ -n "$profile_directory" ]]; then
    profiles_dir="$profile_directory"
  else
    profiles_dir="$(dirname "$0")/profiles"
  fi

  if [[ ! -d "$profiles_dir" ]]; then
    echo "Error: profile directory '$profiles_dir' does not exist"
    exit 1
  fi

  echo ""
  echo "════════════════════════"
  echo "Applying profiles to $name"
  echo "════════════════════════"
  echo ""

  local profile_names profile_name
  IFS=',' read -ra profile_names <<< "$profiles"

  for profile_name in "${profile_names[@]}"; do
    profile_name=$(trim "$profile_name")
    if [[ -z "$profile_name" ]]; then
      continue
    fi

    local profile_path=""
    local ext
    for ext in yaml yml; do
      if [[ -f "$profiles_dir/$profile_name.$ext" ]]; then
        profile_path="$profiles_dir/$profile_name.$ext"
        break
      fi
    done

    if [[ -z "$profile_path" ]]; then
      echo "Error: profile '$profile_name' not found in '$profiles_dir'"
      exit 1
    fi

    echo "Loading profile: $profile_name"
    local result
    result=$(parse_profile_yaml "$profile_path")

    local allow_list deny_list domain
    allow_list=$(echo "$result" | python3 -c "import sys, json; print('\n'.join(json.load(sys.stdin).get('allow', [])), end='')")
    deny_list=$(echo "$result" | python3 -c "import sys, json; print('\n'.join(json.load(sys.stdin).get('deny', [])), end='')")

    if [[ -n "$allow_list" ]]; then
      while IFS= read -r domain; do
        [[ -z "$domain" ]] && continue
        echo "  allowing $domain"
        sbx policy allow network --sandbox "$name" "$domain"
      done <<< "$allow_list"
    fi

    if [[ -n "$deny_list" ]]; then
      while IFS= read -r domain; do
        [[ -z "$domain" ]] && continue
        echo "  denying $domain"
        sbx policy deny network --sandbox "$name" "$domain"
      done <<< "$deny_list"
    fi
  done

  echo ""
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n)
            name="$2"
            shift 2
            ;;
        -p)
            path="$2"
            shift 2
            ;;
        -e)
            envfile="$2"
            shift 2
            ;;
        -s)
            secrets="$2"
            shift 2
            ;;
        -sf)
            secretsfile="$2"
            shift 2
            ;;
        -gsf)
            gensecretsfile="$2"
            shift 2
            ;;
        -v)
            volumes+=("$2")
            shift 2
            ;;
        -profile)
            profiles="$2"
            shift 2
            ;;
        -profile-directory)
            profile_directory="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

if [[ -n "$gensecretsfile" ]]; then
    gsf_dir=$(dirname "$gensecretsfile")
    if [[ ! -d "$gsf_dir" ]]; then
        echo "Error: directory '$gsf_dir' does not exist"
        exit 1
    fi
    generate_secrets_file
    exit 0
fi

if [[ -n "$secretsfile" && ! -f "$secretsfile" ]]; then
    echo "Error: secrets config file '$secretsfile' not found"
    exit 1
fi

if [[ -z "$name" || -z "$path" ]]; then
  echo ""
  echo "══════════════════════════════════════════════════════════════════════════════"
  echo "Missing parameter, you must at least provide a sandbox name and a project path"
  echo "══════════════════════════════════════════════════════════════════════════════"
  echo ""
  usage
fi

if [[ -n "$profile_directory" && ! -d "$profile_directory" ]]; then
  echo "Error: profile directory '$profile_directory' does not exist"
  exit 1
fi

if [[ -n "$profiles" ]]; then
  ensure_pyyaml
fi

build_volume_args
check_project_secrets
create_sandbox
setup_sandbox
update_sbx_policy
apply_profiles
