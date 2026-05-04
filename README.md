# Docker SBX

Tools to install Docker Sandboxes engine + create and manage sandboxes

Work in progress !!!
Use at your own risk !!!

Based on [Docker Sandboxes](https://docs.docker.com/reference/cli/sbx/)

Tested on WSL running Ubuntu 24.04 LTS

All scripts must be in same directory, and must be run from that directory


## Install engine

This must be done only once.

Run `./docker-sbx-install.sh` and answer the questions.

At the end of installation, all network communication from the sandboxes is prohibited, except for system updates

## Create a sandbox

This must be done each time a new sandbox must be created.

Each sandbox is based on 3 main components :
  - Claude Code as AI Agent
  - a shared volume where the project is
  - a network security policy (shared between all sandboxes, only HTTP, HTTPS and SSH protocols are allowed)

Claude Code has got write access to the whole sandbox filesystem, including the shared volume, so DO NOT STORE CREDENTIALS in this project volume
For maximum security, the shared volume should only contain source files and CLAUDE.md.

```
./docker-sbx-create-sandbox.sh -n [sandbox name] -p [absolute path] -e {path to env file} :
  -n : name of the sandbox, mandatory
  -p : absolute path of the project's source files, mandatory
  -e : path to environment file, optional
```

In the end, project's source files should be available within `~/workspace` in the sandbox.

### Examples

Project is in user's directory user's projects/test01 and source files are in projects/test01/src :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01/src
```

Project's source files are in /mnt/c/Users/johndoe/projects/test01/src (WSL) :
```
./docker-sbx-create-sandbox.sh -n test01 -p /mnt/c/Users/johndoe/projects/test01/src
```

Project's source files are in user's projects/test01/src and Anthropic API key is provided in private/.anthropic.api.env :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01/src -e $HOME/private/.anthropic.api.env
```

Project's source files are in user's projects/test01/src and AI backend is an Ollama server running in a container on localhost :
```
./docker-sbx-create-sandbox.sh -n test01 -p $HOME/projects/test01/src -e $HOME/private/ollama.local.env
```

anthropic.api.env :
```
ANTHROPIC_API_KEY=...
```

ollama.local.env :
```
ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ANTHROPIC_AUTH_TOKEN=ollama
```

In case of using a local ollama, make sure to allow its domain in network policy, for above example the command would be :
```
sbx policy allow network host.docker.internal
```


## Use a sandbox

Standard launch command is `sbx run [sandbox name]`, Claude should execute automatically within project's directory.

Run `sbx exec -ti [sandbox name] bash` if you need to take a look or fix something from within the sandbox.
