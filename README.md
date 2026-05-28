# Docker SBX

Tools to install [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/) engine + create and manage AI Agent sandboxes

Work in progress !!!
Use at your own risk !!!

Tested on :

  - WSL Ubuntu 24.04 LTS and Ubuntu 26.04 LTS

  - Ubuntu 26.04 LTS

All scripts must be in same directory, and must be run from that directory


## Install engine

This must be done only once.

First [create a docker account](https://app.docker.com/signup/) if you do not have one already or if you want one for this installation.

Run `./docker-sbx-install.sh` and answer the questions (at one point you will need your docker account).

At the end of installation, all network communication from the sandboxes is prohibited, except for system updates


## Create a sandbox

This must be done each time a new sandbox must be created.

Each sandbox is based on 3 main components :
  - Claude Code as AI Agent
  - a shared volume where the project is
  - a network security policy (shared between all sandboxes, only HTTP, HTTPS and SSH protocols are allowed)

Claude Code has got write access to the whole sandbox filesystem, including the shared volume, so DO NOT STORE CREDENTIALS in this project volume

For maximum security, the shared volume should only contain project files (sources, git, ...) WITHOUT any credentials/certificates/... files.

For the same reason, sources history (git) MUST NOT contain credentials/certificates/... either (or worst-case scenario if those data exist then they must be obsolete).

```
./docker-sbx-create-sandbox.sh -n [sandbox name] -p [absolute path] -e {path to env file} -s {true/false} -sf {path to secrets files settings} -gsf {path to secrets files settings} :
  -n   : name of the sandbox [[mandatory]]
  -p   : absolute path of the project's files [[mandatory]]
  -e   : path to environment file [[optional]]
  -s   : check secrets [[optional, true by default]]
  -sf  : path to secrets files settings [[optional, uses default settings if missing]]
  -gsf : generate a default secrets files settings and exit, parameter is path to secrets files settings [[optional]]
```

In the end, project's files should be available within `~/workspace` in the sandbox.

### Examples

Project is in user's directory user's projects/test01 :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01
```

Project's files are in /mnt/c/Users/johndoe/projects/test01 (WSL) :
```
./docker-sbx-create-sandbox.sh -n test01 -p /mnt/c/Users/johndoe/projects/test01
```

Project's files are in user's projects/test01 and Anthropic API key is provided in private/.anthropic.api.env :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.anthropic.api.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env
```

Project's files are in user's projects/test01 and AI backend is an Ollama server running on a server, using a custom secrets files settings :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01 -e $HOME/private/.ollama.srv.env -sf secrets.json
```

Generate a default secrets files settings json :
```
./docker-sbx-create-sandbox.sh -gsf secrets.json
```

.anthropic.api.env :
```
ANTHROPIC_API_KEY=[your anthropic API key]
```

.anthropic.auth.env :
```
ANTHROPIC_AUTH_TOKEN=[your anthropic account token]
```

.ollama.srv.env :
```
ANTHROPIC_BASE_URL=http{s}://[ollama host]{:[ollama port]}
ANTHROPIC_AUTH_TOKEN=ollama
```

Example for locally dockerized ollama server with port 11434:
```
ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ANTHROPIC_AUTH_TOKEN=ollama
```


## Use a sandbox

Standard launch command is `sbx run [sandbox name]`, Claude should execute automatically within project's directory.

Run `sbx exec -ti [sandbox name] bash` if you need to take a look or fix something from within the sandbox.
