# Home

My Home Assistant deployment.

## Features

### Bindicator

Daily at 4pm, query upcoming refuse collections using local council service API and
light up the living room lamp with the color of any bin(s) if they will be collected in
the next day.

## Requirements

Install required packages

```shell
$ brew bundle
```

## Secrets

Deployment secrets are encrypted with `ansible-vault`.

The vault password is stored in 1password and accessed via the `op` CLI.

Ansible accesses the vault password via tha `op-client.py` adapter script.

To create a secret, run command of the form

```shell
$ cd deployment
$ ansible-vault encrypt_string --vault-id op-client.py "some-secret-string" --name "secret_name"
Encryption successful
secret_name: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          64386635353466656666376263346333336461306639633236333033623862383139313030363233
          3861386365333864386538666339636630653765303764620a616135343034393439393933383035
          66343962613338383135393738636661303634306231396166616364313238343937313063323436
          3634653731663564660a323338386162666134343466316531373039323963303737356366646165
          3561
```

then add the variable to the appropriate file.

## Deployment

1. Create inventory describing home assistant server in the `homeassistant` group.
   ```yaml
   all:
   children:
     homeassistant:
       hosts:
         homeassistant.local:
           ha_root_dir: /root
   ```

2. Deploy configuration.
   ```shell
   cd deployment
   ansible-playbook --vault-id op-client.py -i inventory/local config.yaml
   ```

Notes:
- The deploy playbook uses the `op` CLI to read the Home Assistant API token, so ensure
  `op` is authenticated before running.
- Most config changes use targeted Home Assistant reload services; full restarts are
  reserved for custom components and non-reloadable config (e.g. HTTP/ZHA).
