#!/bin/bash

set -Eeuo pipefail   # Safer Bash: fail on errors, unset vars, pipeline failures

# ----------------------------------------------------------------------
# Default values --------------------------------------------------------
# ----------------------------------------------------------------------
name=""
path=""
envfile=""
secrets="true"

usage() {
  scriptname=`basename "$0"`
  echo "Usage: $scriptname -n [sandbox name] -p [project path] -e [sandbox env file] -s [check secrets]"
  echo "Options:"
  echo "  -n string     Define the name of the new sandbox [mandatory]"
  echo "  -p string     Absolute path of project's directory [mandatory]"
  echo "  -e string     Env file [optional]"
  echo "  -s true/false Check for secrets (default true)"
  echo ""
  echo "  -h, --help Show this help and exit"
  echo ""
  echo "Example(s):"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -e sbx.env"
  echo ""
  exit 1
}

check_project_secrets_files() {
  echo "Looking for secrets files in $path, please wait..."

  secrets=`find "$path" -type f \( -name "google-services.json" -o -name "key.properties" -o -name "*.jks" -o -name "*.cert" -o -name "*.crt" -o -name "*.key" \) -not -path "*/node_modules/*" -not -path "*/bower_components/*"`
  if echo "$secrets" 2> /dev/null | grep -q .; then
    echo "$secrets"
    echo "Error: secrets file(s) detected within this project, make sure those can not be reached from sandbox and run this command again"
    echo ""
    exit 1
  else
    echo "No secrets file detected within this project, let's continue"
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

create_sandbox() {
  prjname=`basename "$path"`

  echo ""
  echo "════════════════"
  echo "Creating sandbox $name"
  echo "════════════════"
  echo ""
  sbx create --name "$name" claude "$path"
}

setup_sandbox() {
  echo ""
  echo "════════════════"
  echo "Updating sandbox $name"
  echo "════════════════"
  echo ""

  sbx cp ./docker-sbx-setup-sandbox.sh "$name":../setup-sandbox.sh
  sbx exec -ti "$name" sudo chmod 755 ../setup-sandbox.sh
  if [[ -z "$envfile" ]]; then
    sbx exec -ti "$name" bash ../setup-sandbox.sh "$path"
  else
    echo "Using env file $envfile"
    sbx exec -ti --env-file "$envfile" "$name" bash ../setup-sandbox.sh "$path"
  fi
}

while getopts ":n:p:e:s:h" opt; do
    case "$opt" in
        n) name="$OPTARG";;
        p) path="$OPTARG";;
        e) envfile="$OPTARG";;
        s) secrets="$OPTARG";;
        h) usage;;
    esac
done

if [[ -z "$name" || -z "$path" ]]; then
  echo ""
  echo "══════════════════════════════════════════════════════════════════════════════"
  echo "Missing parameter, you must at least provide a sandbox name and a project path"
  echo "══════════════════════════════════════════════════════════════════════════════"
  echo ""
  usage
fi

check_project_secrets
create_sandbox
setup_sandbox
