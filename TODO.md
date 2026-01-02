# TODO

- Fix/rewrite covers solar management (close/open). The reopen logic in `deployment/files/automations/covers_solar_open.yaml` appears broken and likely never selects covers to reopen.
- Switch `sensor` and `template` includes to `!include_dir_merge_list` and consolidate related entries (e.g., `sensors/waste_collection_schedule_version.yaml`, `templates/waste_collection_schedule_version.yaml`).
- Investigate how to align mobile_app notify service prefixes with renamed phone entity IDs.
- Add debounce/lockout strategy for spare-solar-triggered automations to prevent simultaneous starts.
- Octopus Energy entity IDs are dynamic (account/MPAN variables), so `deployment/files/pyscript/washing_machine_costing.py` should become a Jinja2 template.
- Reintroduce washing machine power profile and latest finish helpers once pyscript or another parser is available.
