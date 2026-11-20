---
name: docker-sbx-example
description: Example skill installed by the docker-sbx "example" profile.
---

This skill was installed at agent level by `profiles/example/`.

It exists to demonstrate that a profile directory can carry a skill: any
top-level entry other than `profile.yaml`, `agent/` and `project/` is copied
into `/home/agent/.claude/` inside the sandbox, mirroring this directory.
