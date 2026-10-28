# Direct remote access

`https://home.newtonho.me` connects directly to Home Assistant. DNS stays at
Namecheap. The router forwards TCP 443 to HA port 443, and TCP 80 to HA port 80
for ACME HTTP validation. Port 80 belongs to the Let's Encrypt app's challenge
responder, not the Home Assistant login/API.

## Dynamic DNS

Enable Dynamic DNS under Namecheap → `newtonho.me` → Advanced DNS. Configure
the existing `home` A record for Dynamic DNS, retaining its current address.
Do not change the domain's nameservers or mail records.

In Home Assistant → Settings → Devices & services, add **Namecheap DynamicDNS**:

- Host: `home`
- Domain: `newtonho.me`
- Password: the separate Namecheap Dynamic DNS password, stored in 1Password.

This integration updates the address every five minutes. It uses Namecheap's
DDNS service rather than the general DNS API, so changing the WAN address does
not require maintaining an API IP whitelist. DNS caches can extend the brief
interruption after an IP change. This setup updates IPv4 only; do not add an
AAAA record without arranging an IPv6 updater and checking IPv6 firewall rules.

Integration credentials are stored in HA's managed config entries, not YAML.
Recreate this integration after restoring a deployment without HA's storage.

## Certificates

The existing `core_letsencrypt` app is configured with:

```yaml
domains:
  - home.newtonho.me
challenge: http
dns: {}
certfile: fullchain.pem
keyfile: privkey.pem
```

Retain the existing ACME email and other app defaults. Keep the app's TCP 80
mapping enabled, and leave force-renew/staging options disabled for normal use.
The renewal automation starts the app daily at 03:00; it checks whether renewal
is due, then exits. It no longer watches the router IP sensor or uses Namecheap's
general API credentials. App options are managed through Supervisor, outside
this repo's normal configuration-file deployment.

HA loads its TLS certificate at HTTP server startup. The command-line sensor
`sensor.home_tls_certificate_on_disk` polls the certificate's expiry every five
minutes. When it changes to a later, unexpired date, an automation restarts HA
to load it. This interrupts active automations and access briefly, normally
following the overnight renewal. Unknown/unavailable startup states do not
trigger restarts. If the command-line sensor is unavailable during renewal,
inspect the certificate and restart manually after fixing the sensor.

The built-in **Certificate Expiry** integration monitors `home.newtonho.me:443`.
Its entity must be `sensor.home_newtonho_me_cert_expiry`, as referenced by the
health automation. A persistent HA notification and a push to Connor's phone
warn at 09:00 daily, or after an hour of unhealthy state, if the served
certificate cannot be validated or expires within 14 days. A healthy state
clears the persistent warning. These are local
HA notifications and app pushes, not an independent external uptime monitor.

## Verify and recover

1. Check Namecheap DDNS integration setup and that public DNS matches the WAN IP.
2. Start the Let's Encrypt app and inspect its logs for successful validation.
   A stopped app is normal after completion; inspect logs to distinguish success
   from failure.
3. Allow up to five minutes for the disk sensor to detect renewal and restart HA.
4. Check the certificate presented by HTTPS, not just the file on disk:

   ```sh
   curl -I https://home.newtonho.me
   openssl s_client -connect home.newtonho.me:443 -servername home.newtonho.me </dev/null 2>/dev/null |
     openssl x509 -noout -dates -subject
   ```

5. Confirm access over a separate connection, such as a phone on mobile data.
   A check from inside the house alone does not prove the router's external path.

If renewal fails, use SSH to `root@homeassistant.local` and inspect
`ha addons logs core_letsencrypt`. Verify inbound TCP 80 and public DNS before
retrying; avoid repeated forced production issuance. Keep HTTPS enabled during
recovery. `ha core check` followed by `ha core restart` loads a valid replacement
certificate if automatic restart was missed.

References: [Namecheap DDNS integration](https://www.home-assistant.io/integrations/namecheapdns/),
[Let's Encrypt app](https://github.com/home-assistant/apps/blob/master/letsencrypt/DOCS.md).

## Deploy these files only

From `deployment/`, run:

```sh
ansible-playbook --vault-id op-client.py -i inventory/local config.yaml --tags remote_access
```

This copies only the certificate automations and disk sensor, checks the HA
configuration and reloads the affected integrations if files changed. It does
not synchronize the other automation or custom-component directories. App
options and the two UI-managed integrations are configured separately above.

## Commissioning: 28 October 2026

- Namecheap DynamicDNS was created successfully and performed its initial update
  for `home.newtonho.me`; the integration reports loaded.
- After enabling the router's TCP 80 forward, Let's Encrypt successfully
  completed external HTTP validation and issued a production certificate.
- Refreshing the disk sensor triggered the certificate-loading automation at
  12:14:56 UTC. HA restarted and loaded the replacement certificate.
- Trusted HTTPS GET returned 200. The disk and served-certificate sensors both
  report expiry `2027-01-26T11:15:55+00:00`; the latter reports `is_valid: true`.
- HA configuration validation and the scoped Ansible deploy passed. The deploy
  found both files already matching the directly installed versions.
- Recovery after a real WAN-IP change has not been forced, and interactive
  access over mobile data has not been checked by the user.

The main continuing security exposure is public HTTPS access to HA itself.
Use strong unique account passwords, enroll MFA for every human account and
keep HA/apps updated. During commissioning, neither of the two active human
accounts had MFA enrolled, and HA reported an update available from 2026.9.3
to 2026.10.4. Account security and software updates were not changed here.
