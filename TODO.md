# TODO

- [ ] Fix/rewrite covers solar management (close/open). The reopen logic in
      `deployment/files/automations/covers_solar_open.yaml` appears broken and
      likely never selects covers to reopen.

- [ ] Investigate why Ansible deployment runs are slow and optimize where possible.

- [ ] Switch `sensor` and `template` includes to `!include_dir_merge_list` and
      consolidate related entries (e.g.,
      `sensors/waste_collection_schedule_version.yaml`,
      `templates/waste_collection_schedule_version.yaml`).

- [ ] Investigate how to align mobile_app notify service prefixes with renamed phone
      entity IDs.

- [ ] Add debounce/lockout strategy for spare-solar-triggered automations to prevent
      simultaneous starts.

- [ ] Deprecate/remove double-tap Bindicator automation (family room lamp button).

- [ ] Switch washing machine costing profile input to a named profile selector (eg,
      input_select) backed by a code-defined catalog; add a profile for
      "AI Wash + Dry (90m)" and use the selected profile to drive cost sensors.

- [x] Octopus Energy entity IDs are dynamic (account/MPAN variables), so
      `deployment/files/pyscript/washing_machine_costing.py` should become a Jinja2
      template.

- [x] Reintroduce washing machine power profile and latest finish helpers once pyscript
      or another parser is available.

- [x] Update zigpy config to replace deprecated `z2m_index` with `extra_providers`
      (repeated warnings in `home-assistant.log.old`).

- [x] Fix `automation.dishwasher_done_notification` unknown action `notify.all_phones`
      (service removed or typo).
