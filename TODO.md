# TODO

- [x] Implement and verify the [IOG settlement-period battery charging guard](docs/tasks/T0051-iog-settlement-charge-guard.md): 0.2.2 deployed, 224 tests passed, physical discharge confirmed.
- [ ] Complete natural IOG guard commissioning: new qualifying smart charge, boundary renewal/expiry and overnight ordinary/cycle recharge; see [commissioning record](docs/house-battery-commissioning-log.md).
      Scheduled bonus rates are not proof of qualifying EV charging in each
      half-hour; cover ordinary charging and full-SOC cycle recharge.

- [ ] Fix/rewrite covers solar management (close/open). The reopen logic in
      `deployment/files/automations/covers_solar_open.yaml` appears broken and
      likely never selects covers to reopen.

- [x] Investigate why Ansible deployment runs are slow and optimize where possible.

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

- [ ] Tidy energy_cost_forecast SensorEntityDescription keys before a full release
      (align internal keys with current sensor names to reduce confusion).

- [x] Octopus Energy entity IDs are dynamic (account/MPAN variables), so
      `deployment/files/pyscript/washing_machine_costing.py` should become a Jinja2
      template.

- [x] Reintroduce washing machine power profile and latest finish helpers once pyscript
      or another parser is available.

- [x] Update zigpy config to replace deprecated `z2m_index` with `extra_providers`
      (repeated warnings in `home-assistant.log.old`).

- [x] Fix `automation.dishwasher_done_notification` unknown action `notify.all_phones`
      (service removed or typo).
