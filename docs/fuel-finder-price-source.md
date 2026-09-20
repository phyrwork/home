# Fuel Finder price source

`sensor.bar_hill_tesco_petrol_price` now reads E10 petrol for Tesco Bar Hill
(CB23 8EL) from the official Fuel Finder CSV. Its entity ID and GBP/L unit remain
unchanged, preserving the EV fuel-equivalent rate, savings tracker and dashboard.
The legacy Tesco JSON feed was still returning £1.569/L with an April timestamp;
the replacement reported £1.709/L effective 18 September 2026 at deployment.

The command-line sensor runs `/config/command_line/fuel_finder.py` every 12 hours.
It follows the public developer portal's download flow: obtain a public download
cookie, request CSV metadata, decode the portal's response envelope, then fetch
its fresh signed download URL. It requires no account or API credentials. The
public client encoding constant is bundled in the portal JavaScript, not a private
secret. This is the portal's download interface, not the authenticated public API;
a future portal change may require updating the fetcher.

The script selects a unique Tesco row at CB23 8EL, validates E10, converts pence
to pounds and records both the price effective date and CSV generation time. It
does not substitute E5 if E10 is missing. Responses are obtained over verified
HTTPS; signed URLs and cookie tokens are never logged or stored in the cache.

A successful result is atomically saved to `/config/fuel_finder_bar_hill.json`.
If the next download or parse fails, the script returns that price, keeps its
original `last_updated` and `last_successful_fetch`, and reports `fetch_status:
cached` with an error type. Without a valid cache, it fails rather than inventing
a price. No historical prices or savings are backfilled.

Attributes:

- `last_updated`: E10 price effective time in UTC (used by the Fuel Basis card).
- `price_submitted`: time submitted to Fuel Finder.
- `csv_generated_at`: publication time of the downloaded CSV.
- `last_successful_fetch`, `last_attempt`: polling timestamps.
- `fetch_status`: `ok` or `cached`; `fetch_error` identifies failures.
- `station`, `postcode`, `fuel_type`, `source`: provenance.

The old REST registry entry is replaced by the command-line entry under the same
entity ID. Historical recorder rows remain intact. The old REST YAML is removed.

Validation: `scratch/test_fuel_finder.py` tests price/date parsing, missing or
ambiguous stations, invalid prices, first-fetch failure and cached fallback.
A real download also verified the complete public download flow.

Source: https://www.developer.fuel-finder.service.gov.uk/fuel-finder/access-latest-fuelprices

Migration used one-time `rest.reload` and `command_line.reload` calls; HA was not
restarted. No deployment playbook/handler changes are needed or retained. The
Power dashboard was verified showing £1.709/L and £0.6476/kWh, with the effective
time correctly displayed in local time.
