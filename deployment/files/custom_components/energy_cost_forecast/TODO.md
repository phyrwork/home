# TODO

- [x] Remove start_by feature: delete config option, strings/translations, helpers, and any related attributes.
- [x] Add update_interval refresh in coordinator (default 15m) and make it configurable (0-1440 minutes, 0 disables polling).
- [x] Update Start Later Costs sensor state handling: make start_now_time a proper timestamp (device_class TIMESTAMP) or move to attribute only; remove the misleading rates attribute from later.
- [x] Tighten YAML config validation: target_percentile 0-100, start_step_minutes 0-1440, entity IDs use cv.entity_id.
- [x] Rename max_cost_percentile to target_percentile everywhere (no backwards compatibility).
- [x] Documentation checklist for manifest.json:
  - [x] Replace placeholder documentation URL.
  - [x] Add README for integration with configuration examples, required rate schema, profile formats, and limitations.
  - [ ] Add release notes/migration notes for renamed config keys.
  - [ ] Add codeowners entry.
- [x] Tests: add pytest coverage using pytest-homeassistant-custom-component for parsing, cost calculations, update_interval behavior, and sensor attributes.
