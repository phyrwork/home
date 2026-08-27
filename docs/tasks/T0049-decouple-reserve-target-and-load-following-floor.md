# T0049 — Decouple reserve target from load-following floor

Status: Deployed — local validation complete; live acceptance pending

Supersedes T0041 statements that set the global Solis Battery Reserve SOC to
the dynamic model reserve.

## Defect

The controller used the same quantized reserve SOC for two different jobs:

1. the target SOC of a forced reserve-export discharge slot; and
2. the global Battery Reserve SOC used by ordinary Peak Shaving/load following.

Live evidence on 2026-08-27 showed actual battery energy `6.752256 kWh` at
`21%`, an exact reserve target of `6.44077738538028 kWh`, and
`3.22541738538028 kWh` of planned reserve above the 10% safety floor. The
forced slot was correctly off at the quantized 21% target, but the global
Battery Reserve SOC was also 21%. House demand was approximately 323 W while
battery output was only approximately 36 W. The inverter was forbidden from
using the planned reserve for the house.

## Control contract

Keep the two native controls independent:

- `control_reserve_soc_percent` is the upward-quantized dynamic model target.
  It drives reserve-export eligibility and the target SOC of reserve-export
  and full-SOC-cycle discharge slots.
- `battery.minimum_soc_percent` is the global Battery Reserve SOC. It remains
  the fixed load-following floor, currently `MINIMUM_SOC_PERCENT` / 10%.
- Battery Reserve remains enabled during managed Feed-In Priority operation.
- `RESERVE_FOLLOW` has no native slot and enables Peak Shaving so actual house
  demand consumes the planned reserve toward the fixed safety floor.

The global Battery Reserve capability is not part of the dynamic slot-target
quantization domain. Dynamic targets need only be representable by the native
discharge-slot target capabilities that use them. The adapter still validates
and quantizes the fixed Battery Reserve floor against its own live capability.

No persistence, helper entity, transition object or additional reserve value
is required. `Reserve (Usable)` remains the planned reserve target above the
safety floor; `Reserve Balance` remains actual battery energy minus the exact
model target.

## Local acceptance

- A 21% reserve-export slot target coexists with a 10% global Battery Reserve
  SOC.
- A coarse Battery Reserve capability cannot round or otherwise alter the
  dynamic discharge-slot target.
- Controller reconciliation always passes the configured minimum SOC to the
  global Battery Reserve policy even when the plan's dynamic reserve is higher.
- Forced-discharge selection and completion continue to use the dynamic
  quantized target.
- Focused planner, Solis adapter, controller and sensor tests pass.

Local evidence: all 148 house-battery component tests and all 53 deployment
tests pass; component sources compile and `git diff --check` is clean.

## Live acceptance

1. Deploy and prove global Battery Reserve SOC becomes 10% while the dynamic
   reserve diagnostics and inactive/active discharge-slot target remain at the
   independently calculated value.
2. With SOC above 10%, all native slots off and `RESERVE_FOLLOW` active, prove
   battery output follows house demand for at least five fresh samples.
3. Prove a later reserve-export slot still stops at the dynamic quantized target
   rather than 10%.

The repeated Grid Peak Shaving `off -> on` control churn observed during the
same diagnosis is a separate defect and is outside this card.

## Deployment — 2026-08-27

The first deployment attempt did not start because the Ansible vault helper's
1Password session had expired. After the prescribed single sign-in and retry,
Ansible obtained the vault secret but SSH key-agent signing failed before host
connection. The play recap was `ok=0`, `changed=0`, `unreachable=1`; no Home
Assistant files or settings changed during that attempt.

After SSH key-agent reauthorization, the same full playbook completed with
`ok=140`, `changed=4`, no failures and successful pre-restart Home Assistant
configuration validation. The custom component and bytecode cache were replaced
and Home Assistant restarted. Final live entity and physical power-flow proof
remains pending because the subsequent Home Assistant API-token read failed
after the prescribed 1Password sign-in and single retry.
