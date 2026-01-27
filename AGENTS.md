# AGENTS

## Home Assistant access

- SSH: `root@homeassistant.local`
  - Example: `ssh root@homeassistant.local`
- API token (long-lived access token): stored in 1Password
  - Read with: `op read "op://jxs6qrivegu7ekpzkt27seurvy/fgvzkd432x4xsjq7f3vu4zippu/password"`
  - Note: the HA TLS cert is for `home.newtonho.me`, so `https://homeassistant.local` may require skipping verification or using the `home.newtonho.me` hostname.

## Deployment

- See `README.md` for full instructions.
- Quick deploy:
  ```sh
  cd deployment
  ansible-playbook --vault-id op-client.py -i inventory/local config.yaml
  ```
- Inventory default user is root (see `deployment/inventory/local/00-main.yaml`).

## Automation conventions

- YAML automations live in `deployment/files/automations/*.yaml`.
  - Each file contains a YAML list of automations (`- id: ...`).
  - Keep related automations together (e.g., `closet_light.yaml` for closet motion + button behavior).
- Templated automations live in `deployment/templates/automations/*.yaml.j2` and are rendered by Ansible.
- Use existing files as reference for style and structure; prefer adding to the most specific file rather than creating a new one.
- Home Assistant derives the automation `entity_id` from the `alias` (friendly name). If you reference an automation in templates (e.g., `state_attr('automation.<entity_id>', 'last_triggered')`), make sure the alias stays stable.
- For `deployment/files/automations/.rsync-filter`:
  - Add a `P <file>.yaml` entry when a templated automation is rendered into `deployment/files/automations/`.
  - Remove the entry when the file is no longer templated (or should be deletable by sync).

## Development process

### Committing

- When committing, check `TODO.md` and tick off items that are fully resolved by the change.

### Iteration speed

- When developing files (e.g., config, scripts), push iterations directly (e.g., scp) and reload directly (e.g., via API). When complete, if a new file for deployment was added or deployment code changed, then run the Ansible deployment and verify.

### Subtree sync (energy_cost_forecast)

- Local integration repo: `~/src/ha-energy-cost-forecast`
- Subtree prefix: `deployment/files/custom_components/energy_cost_forecast`
- Push subtree to integration `home` branch:
  ```sh
  git subtree push --prefix deployment/files/custom_components/energy_cost_forecast ~/src/ha-energy-cost-forecast home
  ```
- Then push integration `home` → `main`:
  ```sh
  cd ~/src/ha-energy-cost-forecast
  git checkout main
  git merge home
  git push origin main
  ```

## Development deploy gotchas

- When doing dev-mode iteration via scp/ssh, never `rsync --delete` whole directories that include templated files (notably `deployment/files/automations`, `deployment/files/templates`, `deployment/files/sensors`). That will delete rendered files on the HA host.
- For dev-mode, only copy the specific file(s) you changed, or run the full Ansible deploy if you need a full sync.
