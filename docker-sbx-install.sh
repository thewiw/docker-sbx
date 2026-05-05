#!/bin/bash

check_root_privileges() {
  if [[ $EUID -ne 0 ]]; then
      echo "════════════════════════════════════════════════════"
      echo "⛔ Error: This script must be run as root (use sudo)"
      echo "════════════════════════════════════════════════════"
      exit 1
  fi
}

# =================================================
# 🔍 CHECK IF DOCKER EXISTS AND ASK USER IF IT DOES
# =================================================
query_install_docker() {
  if command -v docker &>/dev/null; then
    DOCKER_PATH=$(command -v docker)
    DOCKER_VER=$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "inconnue")

    echo "═══════════════════════════════════════════════════════"
    echo "⚠️  LLERT  : Docker is already installed on this system"
    echo "📍 Binary  : $DOCKER_PATH"
    echo "📦 Version : $DOCKER_VER"
    echo "═══════════════════════════════════════════════════════"
    echo ""

    # ⏱️ Ptnteractive prompt
    echo "⚠️BBEWARE Current docker installation will be removed, including all images and containers !!!"
    if ! read -r -p "🔍 Do you still want to continue the installation [y/N] " RESPONSE; then
      echo "⏱️  Timeout or bad input. Installation canceled."
      echo ""
      exit 1
    fi

    case "${RESPONSE,,}" in
      o|y|yes|oui)
        echo "✅ Installation authorized. Continuing installation...\n"
        echo ""
        ;;
      *)
        echo "❌ Installation canceled by user."
        echo ""
        exit 1
        ;;
    esac
  fi
}

install_docker() {
  echo "════════════════════════════════════════════════════════"
  echo "⚙️  ocker installation in progress, please be patient..."
  echo "════════════════════════════════════════════════════════"
 
  # Clean former or alternative Docker
  sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc | cut -f1)

  # Add Docker's official GPG key:
  sudo apt update
  sudo apt install ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  # Add the repository to Apt sources:
  filepath=/etc/apt/sources.list.d/docker.sources
  sudo rm $filepath 2> /dev/null
  sudo touch $filepath
  sudo echo 'Types: deb' 2> $filepath
  sudo echo 'URIs: https://download.docker.com/linux/ubuntu' 2> $filepath
  sudo echo 'Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")' 2> $filepath
  sudo echo 'Components: stable' 2> $filepath
  sudo echo 'Architectures: $(dpkg --print-architecture)' 2> $filepath
  sudo echo 'Signed-By: /etc/apt/keyrings/docker.asc' 2> $filepath

  sudo apt update

  # Install Docker
  sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_sbx() {
  echo "════════════════════════════════════════════════════════════════════"
  echo "⚙️  oocker sandboxes installation in progress, please be patient..."
  echo "════════════════════════════════════════════════════════════════════"

  curl -fsSL https://get.docker.com | sudo REPO_ONLY=1 sh
  sudo apt-get install docker-sbx
  sudo usermod -aG kvm $USER
  newgrp kvm
  sbx policy set-default deny-all # Set default policy before login
  sbx login
}

setup_docker_sbx() {
  sbx policy set-default deny-all # Ensure deny-all is enforced

  # sandbox is allowed to do system updates
  sbx policy allow network "archive.ubuntu.com,security.ubuntu.com,download.docker.com"

  # deny some sites "by name", to make it even "surer" (in case default policy is changed later)
  sbx policy deny network "github.com,github.org"
  sbx policy deny network "gitlab.com,gitlab.org"
  sbx policy deny network "atlassian.com,atlassian.net,bitbucket.com,bitbucket.org"
  sbx policy deny network "postman.com"

  # sandbox is allowed to use inference sites
  #sbx policy allow network "api.anthropic.com" # allow Anthropic API
  #sbx policy allow network "host.docker.internal" # allow local docker containers
}

check_root_privileges
query_install_docker
install_docker
install_docker_sbx
setup_docker_sbx
