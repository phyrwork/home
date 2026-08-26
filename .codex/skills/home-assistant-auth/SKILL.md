---
name: home-assistant-auth
description: Use when working in this Home Assistant repo and a task needs deployment auth, Home Assistant API access, SSH to homeassistant.local, Ansible vault auth, or repeated op reads for the HA API token.
---

# Home Assistant Auth

Use this skill for auth-sensitive Home Assistant deployment work in this repo.

## SSH

- SSH target: `root@homeassistant.local`.
- Prefer the dedicated local key configured for `homeassistant.local` in `~/.ssh/config`.
- Do not fall back to HTTPS or alternate SSH identities when SSH auth fails.
- If SSH fails with a real key/agent/auth error, stop and ask Connor to reauthorize or fix the keychain/agent.

## 1Password

- The Ansible vault password and Home Assistant API token come from `op`.
- Before the first `op read` in a new agent/session, run `op signin` so the CLI
  session is authenticated even when the desktop app is already open.
- If `op read` reports that the CLI cannot connect to the desktop app, run
  `op signin` once and retry the read once before asking Connor for help.
- If `op signin` or the retried `op read` fails or times out, stop and ask
  Connor to retry. Do not bypass `op` for the same secret.

## HA API Token Cache

When a task needs the Home Assistant API token more than once, cache it for the current shell/session in a restrictive tempfile instead of repeatedly calling `op read`.

Use this pattern:

```sh
HA_API_TOKEN_FILE="${TMPDIR:-/tmp}/ha-api-token.$USER"
if [ ! -s "$HA_API_TOKEN_FILE" ]; then
  umask 077
  op read "op://jxs6qrivegu7ekpzkt27seurvy/fgvzkd432x4xsjq7f3vu4zippu/password" > "$HA_API_TOKEN_FILE"
fi
export HA_API_TOKEN="$(cat "$HA_API_TOKEN_FILE")"
```

Delete the tempfile when the task is complete if it is no longer needed:

```sh
rm -f "$HA_API_TOKEN_FILE"
```

Never print the token. Use `no_log: true` for Ansible tasks that pass it.

## Deploy

Run deploys from `deployment/`:

```sh
ansible-playbook --vault-id op-client.py -i inventory/local config.yaml
```

If a deploy fails after changing Home Assistant config or custom components but before handlers run, rerun the same playbook after fixing the auth issue so reload/restart handlers can complete.
