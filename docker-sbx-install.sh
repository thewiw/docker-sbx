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

# =================================================
# 🔍 CHECK IF DOCKER EXISTS AND ASK USER IF IT DOES
# =================================================
query_install_docker() {
  if command -v docker &>/dev/null; then
    DOCKER_PATH=$(command -v docker)
    DOCKER_VER=$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "inconnue")

    echo ""
    echo "════════════════════════════════════════════════════"
    echo "ALERT   : Docker is already installed on this system"
    echo "Binary  : $DOCKER_PATH"
    echo "Version : $DOCKER_VER"
    echo "════════════════════════════════════════════════════"
    echo ""

    # ⏱️ Ptnteractive prompt
    echo "BEWARE Current docker installation will be removed, including all images and containers !!!"
    if ! read -r -p "🔍 Do you still want to continue the installation [y/N] " RESPONSE; then
      echo "Timeout or bad input. Installation canceled."
      echo ""
      exit 1
    fi

    case "${RESPONSE,,}" in
      o|y|yes|oui)
        echo "Installation authorized. Continuing installation...\n"
        echo ""
        ;;
      *)
        echo "Installation canceled by user."
        echo ""
        exit 1
        ;;
    esac
  fi
}

install_docker() {
  echo ""
  echo "═════════════════════════════════════════════════════"
  echo "Docker installation in progress, please be patient..."
  echo "═════════════════════════════════════════════════════"

  # Clean former or alternative Docker
  sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc | cut -f1)

  # Add Docker's official GPG key:
  sudo apt update -y
  sudo apt install ca-certificates curl -y
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  # Add the repository to Apt sources:
  filepath=/etc/apt/sources.list.d/docker.sources
  suites=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
  archs=$(dpkg --print-architecture)
  sudo rm $filepath 2> /dev/null
  sudo touch $filepath
  sudo echo "Types: deb" >> $filepath
  sudo echo "URIs: https://download.docker.com/linux/ubuntu" >> $filepath
  sudo echo "Suites: ${suites}" >> $filepath
  sudo echo "Components: stable" >> $filepath
  sudo echo "Architectures: ${archs}" >> $filepath
  sudo echo "Signed-By: /etc/apt/keyrings/docker.asc" >> $filepath

  sudo apt update -y

  # Install Docker
  sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y
}

install_docker_sbx() {
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "Docker sandboxes installation in progress, please be patient..."
  echo "═══════════════════════════════════════════════════════════════"

  curl -fsSL https://get.docker.com | sudo REPO_ONLY=1 sh
  sudo apt-get install docker-sbx -y
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
    sg kvm -c 'sbx policy allow network -g "archive.ubuntu.com,security.ubuntu.com,download.docker.com"'

    # deny some sites "by name", to make it even "surer" (in case default policy is changed later)
    sg kvm -c 'sbx policy deny network -g "github.com,github.org"'
    sg kvm -c 'sbx policy deny network -g "gitlab.com,gitlab.org"'
    sg kvm -c 'sbx policy deny network -g "atlassian.com,atlassian.net,bitbucket.com,bitbucket.org"'
    sg kvm -c 'sbx policy deny network -g "postman.com"'

    # Note (DO NOT UNCOMMENT) : allow specific sandboxes to use some sites
    # sbx policy allow network [sandbox name] "api.anthropic.com" # allow Anthropic API
    # sbx policy allow network [sandbox name] "host.docker.internal" # allow local docker containers
EOFSBX
}

setup_env() {
  echo "export SBX_NO_TELEMETRY=1" >> ~/.bashrc
}


check_root_privileges
query_install_docker
install_docker
install_docker_sbx
setup_docker_sbx
setup_env

echo ""
echo "═════════════════════════════════════════════════════════════════════════════════════════"
echo "Installation should be done, it is recommended to logout/login for changes to take effect"
echo "═════════════════════════════════════════════════════════════════════════════════════════"
echo ""

