#!/bin/bash

curuser="${SUDO_USER}"

check_root_privileges() {
  if [[ $EUID -ne 0 ]]; then
      echo "══════════════════════════════════════════════════"
      echo "Error: This script must be run as root (use sudo)"
      echo "══════════════════════════════════════════════════"
      exit 1
  fi
}

# =====================================================================
# 🔍 INSTALL DOCKER, OR UPDATE IT IN PLACE WHEN ALREADY INSTALLED
# =====================================================================

# Packages installed from Docker's own apt repository, and the conflicting
# packages the official instructions ask to remove first (distro builds of the
# same binaries, which would clash with them).
DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
DOCKER_CONFLICTING_PACKAGES=(docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc)

docker_installed() {
  command -v docker >/dev/null 2>&1
}

# Ask a yes/no question: 0 for yes, 1 for no, EOF or anything else. It never
# exits the script, because a "no" here only skips an optional step.
confirm() {
  local prompt="$1" response

  if ! read -r -p "$prompt" response; then
    echo ""
    return 1
  fi

  case "${response,,}" in
    o|y|yes|oui) return 0 ;;
    *)           return 1 ;;
  esac
}

# Point apt at Docker's official repository. Idempotent, so it is called both
# for a fresh install and before upgrading an existing one.
setup_docker_repo() {
  # Add Docker's official GPG key:
  sudo apt update -y
  sudo apt install ca-certificates curl -y
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  # Add the repository to Apt sources. The file is written through "sudo tee"
  # rather than "sudo echo ... >>": that redirection is performed by the
  # unprivileged shell, which only happens to work on a freshly touched file.
  local filepath=/etc/apt/sources.list.d/docker.sources
  local suites archs
  suites=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
  archs=$(dpkg --print-architecture)
  sudo rm -f "$filepath"
  {
    echo "Types: deb"
    echo "URIs: https://download.docker.com/linux/ubuntu"
    echo "Suites: ${suites}"
    echo "Components: stable"
    echo "Architectures: ${archs}"
    echo "Signed-By: /etc/apt/keyrings/docker.asc"
  } | sudo tee "$filepath" >/dev/null
}

remove_conflicting_docker_packages() {
  # Remove only what is actually installed: "apt remove" with no package names
  # is an error, and these packages are absent on most machines.
  local present
  present=$(dpkg --get-selections "${DOCKER_CONFLICTING_PACKAGES[@]}" 2>/dev/null | awk '$2 == "install" { print $1 }')
  if [[ -n "$present" ]]; then
    # shellcheck disable=SC2086  # word splitting is intended: one name per word
    sudo apt remove -y $present
  fi
}

install_docker() {
  echo ""
  echo "═════════════════════════════════════════════════════"
  echo "Docker installation in progress, please be patient..."
  echo "═════════════════════════════════════════════════════"

  # Clean former or alternative Docker
  remove_conflicting_docker_packages

  setup_docker_repo

  sudo apt update -y

  # Install Docker
  sudo apt install "${DOCKER_PACKAGES[@]}" -y

  sudo usermod -aG docker "${curuser}"
}

update_docker() {
  echo ""
  echo "═════════════════════════════════════════════════════"
  echo "Docker update in progress, please be patient..."
  echo "═════════════════════════════════════════════════════"

  # Ensure the official repository is configured, then upgrade in place.
  # --only-upgrade keeps this from pulling in anything that is not already
  # installed, so an existing setup is never replaced by a different build.
  setup_docker_repo

  sudo apt update -y
  sudo apt install --only-upgrade "${DOCKER_PACKAGES[@]}" -y

  sudo usermod -aG docker "${curuser}"
}

# Fresh install when Docker is absent, in-place update when it is present. A
# refusal to update is not an error: the rest of the installation carries on.
install_or_update_docker() {
  if ! docker_installed; then
    install_docker
    return 0
  fi

  local path version
  path=$(command -v docker)
  version=$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")

  echo ""
  echo "════════════════════════════════════════════════════"
  echo "Docker is already installed on this system"
  echo "Binary  : $path"
  echo "Version : $version"
  echo "════════════════════════════════════════════════════"
  echo ""
  echo "It will be updated in place: existing images and containers are kept."

  if confirm "🔍 Update the existing Docker installation [y/N] "; then
    echo ""
    update_docker
  else
    echo "Docker update skipped, continuing with the rest of the installation."
    echo ""
  fi
}

install_docker_sbx() {
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "Docker sandboxes installation in progress, please be patient..."
  echo "═══════════════════════════════════════════════════════════════"

  curl -fsSL https://get.docker.com | sudo REPO_ONLY=1 sh
  sudo apt-get install util-linux-extra docker-sbx python3-yaml -y
  sudo usermod -aG kvm "${curuser}"
  su - "${curuser}" <<-EOFSBX
    sg kvm -c 'sbx login'
EOFSBX
}

setup_docker_sbx() {
  su - "${curuser}" <<-EOFSBX
    # ensure deny-all is enforced
    sg kvm -c 'sbx policy set-default deny-all'

    # sandboxes are allowed to do system updates
    sg kvm -c 'sbx policy allow network "archive.ubuntu.com,security.ubuntu.com,download.docker.com"'

    # deny some sites "by name", to make it even "surer" (in case default policy is changed later)
    sg kvm -c 'sbx policy deny network "github.com,github.org"'
    sg kvm -c 'sbx policy deny network "gitlab.com,gitlab.org"'
    sg kvm -c 'sbx policy deny network "atlassian.com,atlassian.net,bitbucket.com,bitbucket.org"'
    sg kvm -c 'sbx policy deny network "postman.com"'

    # Note (DO NOT UNCOMMENT) : allow specific sandboxes to use some sites
    # sbx policy allow network [sandbox name] "api.anthropic.com" # allow Anthropic API
    # sbx policy allow network [sandbox name] "host.docker.internal" # allow local docker containers
    # sbx policy allow network [sandbox name] "{specific ip or specific hostname or specific domain}" # allow some specific machines
EOFSBX
}

setup_env() {
  echo 'export SBX_NO_TELEMETRY=1' >> ~/.bashrc
  echo 'export LANG="C.utf8"' >> ~/.bashrc
  echo 'export LC_ALL="C.utf8"' >> ~/.bashrc
}


check_root_privileges
install_or_update_docker
install_docker_sbx
setup_docker_sbx
setup_env

echo ""
echo "═════════════════════════════════════════════════════════════════════════════════════════"
echo "Installation should be done, it is recommended to logout/login for changes to take effect"
echo "═════════════════════════════════════════════════════════════════════════════════════════"
echo ""
