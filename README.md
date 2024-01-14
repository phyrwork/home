# Home

My Home Assistant apps and deployment.

## Features

### Bindicator

Daily at 5pm, query upcoming refuse collections using local council service API and
light up the living room lamp with the color of any bin(s) if they will be collected in
the next day.

## Deployment

1. Create inventory describing home assistant server in the `homeassistant` group.
   ```yaml
   all:
   children:
     homeassistant:
       hosts:
         homeassistant.local:
           appdaemon_dir: /root/addon_configs/a0d7b954_appdaemon
   ``` 
2. `make -C apps` to build my apps Python `requirements.txt`.

3. (For now) manually add these requirements to the Home Assistant AppDaemon python
   packages and restart AppDaemon.

4. Deploy apps.
   ```shell
   cd deployment
   ansible-playbook -i inventories/local.yaml playbooks/apps.yaml
   ```
