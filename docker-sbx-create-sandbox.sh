#!/bin/bash

set -Eeuo pipefail   # Safer Bash: fail on errors, unset vars, pipeline failures

# ----------------------------------------------------------------------
# Default values --------------------------------------------------------
# ----------------------------------------------------------------------
name=""
path=""
envfile=""

usage() {
  scriptname=`basename "$0"`
  echo "Usage: $scriptname -n [sandbox name] -p [project path] -e [sandbox env file]"
  echo "Options:"
  echo "  -n string  Define the name of the new sandbox [mandatory]"
  echo "  -p string  Absolute path of project's directory [mandatory]"
  echo "  -e string  Env file [optional]"
  echo ""
  echo "  -h, --help Show this help and exit"
  echo ""
  echo "Example(s):"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test"
  echo "  from WSL : $scriptname -n test -p /mnt/c/Projects/test -e sbx.env"
  echo ""
  exit 1
}

create_sandbox() {
  prjname=`basename "$path"`

  echo "════════════════════"
  echo "⚙️  rreating sandbox $name"
  echo "════════════════════"
  sbx create --name "$name" claude "$path"
}

setup_sandbox() {
  echo "════════════════════"
  echo "⚙️  rpdating sandbox $name"
  echo "════════════════════"

  sbx cp ./docker-sbx-setup-sandbox.sh "$name":../setup-sandbox.sh
  sbx exec -ti "$name" chmod 755 ../setup-sandbox.sh
  if [[ -z "$envfile" ]]; then
    sbx exec -ti "$name" bash ../setup-sandbox.sh "$path"
  else
    echo "Using env file $envfile"
    sbx exec -ti --env-file "$envfile" "$name" bash ../setup-sandbox.sh "$path"
  fi
}

while getopts ":n:p:e:h" opt; do
    case "$opt" in
        n) name="$OPTARG";;
        p) path="$OPTARG";;
        e) envfile="$OPTARG";;
        h) usage;;
    esac
done

if [[ -z "$name" || -z "$path" ]]; then
  echo "══════════════════════════════════════════════════════════════════════════════════"
  echo "⚠️  Missing parameter, you must at least provide a sandbox name and a project path"
  echo "══════════════════════════════════════════════════════════════════════════════════"
  echo ""
  usage
fi

create_sandbox
setup_sandbox
