# TODO

- Fix/rewrite covers solar management (close/open). The reopen logic in `deployment/files/automations/covers_solar_open.yaml` appears broken and likely never selects covers to reopen.
- Switch `sensor` and `template` includes to `!include_dir_merge_list` and consolidate related entries (e.g., `sensors/waste_collection_schedule_version.yaml`, `templates/waste_collection_schedule_version.yaml`).
