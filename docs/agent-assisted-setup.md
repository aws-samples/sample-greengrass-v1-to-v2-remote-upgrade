# Agent-assisted setup and debugging (optional)

Everything in the [runbook](../README.md) can be followed by hand. This document describes an optional aid: running an AI coding agent **on the Greengrass device** so it can read logs, run diagnostics, and walk you through Greengrass setup and troubleshooting interactively. It's most useful if you're new to Greengrass or debugging a device that won't behave.

This is separate from the upgrade Lambdas

---

## What it is

The [AWS IoT Greengrass context pack](https://github.com/aws-greengrass/greengrass-agent-context-pack) is a set of structured guides an AI agent auto-loads (via `AGENTS.md`) when your task involves Greengrass. Installed alongside an agent CLI on the device, it gives you an assistant that already knows Greengrass conventions, common failure modes, and the setup flow.

> The context pack targets Greengrass **V2 / Nucleus** experimentation and is used at your own risk

## Install it

The full walkthrough lives in the pack's own [`AGENT-SETUP.md`](https://github.com/aws-greengrass/greengrass-agent-context-pack/blob/main/AGENT-SETUP.md).

## Examples of where it helps

- Diagnosing a V1 core that won't start. Point it at `/greengrass/ggc/var/log/system/runtime.log`
- Reading Phase 3 triage files after an upgrade: (`/var/lib/gg-v2-upgrade/upgrade-status`, `upgrade.log`, and `journalctl -u gg-v1-to-v2-upgrade`).
- Interpreting a Phase 2 readiness report that came back `FAIL`.
- Architecture/tooling mismatches like the AWS CLI `Exec format error`.

## Safety

The agent runs shell commands, and on this device that means as root. Review anything that touches `/greengrass`, systemd units, or cloud resources before approving it, use sandbox AWS credentials, and never attach an agent to a production device.
