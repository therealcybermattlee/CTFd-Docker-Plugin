# CTFd Docker Containers Plugin

This CTFd plugin allows you to run ephemeral Docker containers for specific challenges. Teams can request a container to use as needed, and its lifecycle will be managed by the plugin.

## Usage

Place this plugin in your CTFd/plugins directory. The name of the directory MUST be "containers" (so if you cloned this repo, rename "CTFd-Docker-Plugin" to "containers").

To configure the plugin, go to the admin page, click the dropdown in the navbar for plugins, and go to the Containers page. Then you can click the settings button to configure the connection. You will need to specify some values, including the connection string to use. This can either be the local Unix socket, or an SSH connection. If using SSH, make sure the CTFd host can successfully SSH into the Docker target (i.e. set up public key pairs). The other options are described on the page. After saving, the plugin will try to connect to the Docker daemon and the status should show as an error message or as a green symbol.

To create challenges, use the container challenge type and configure the options. It is set up with dynamic scoring, so if you want regular scoring, set the maximum and minimum to the same value and the decay to zero.

If you need to specify advanced options like the volumes, read the [Docker SDK for Python documentation](https://docker-py.readthedocs.io/en/stable/containers.html) for the syntax, since most options are passed directly to the SDK.

When a user clicks on a container challenge, a button labeled "Get Connection Info" appears. Clicking it shows the information below with a random port assignment.

![Challenge dialog](dialog.png)

A note, we used hidden teams as non-school teams in PCTF 2022 so if you want them to count for decreasing the dynamic challenge points, you need to remove the `Model.hidden == False,` line from the `calculate_value` function in `__init__.py`.

## Azure Container Instances backend

This fork supports running challenge containers on **Azure Container Instances (ACI)** instead of (or alongside) a Docker daemon. Pick **Azure Container Instances** under Backend on the settings page to switch.

### Azure prerequisites

1. **Resource group** for challenge container groups, e.g. `ctfd-challenges`.
2. **Azure Container Registry** with your private images.
3. **User-assigned managed identity** (referred to as the *puller* UAMI) with the **AcrPull** role on the ACR. This UAMI is attached to every spawned container group so it can pull private images without storing registry credentials in CTFd.
4. **An identity for CTFd itself** that can call ARM. Two options:
   - If CTFd runs on Azure (ACI, AKS, VM, App Service): assign it a managed identity with **Contributor** on the challenge resource group and **AcrPull** on the ACR.
   - If CTFd runs elsewhere (e.g. Docker on a laptop): create a **service principal** and set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` in CTFd's environment. The plugin uses `DefaultAzureCredential` so any of these auth methods are picked up automatically.

### Plugin settings

Fill in on the Settings page (only the Azure section is required when the backend is `aci`):

- **Subscription ID** — your Azure subscription
- **Resource Group** — where container groups are created (must already exist)
- **Region** — single region for all challenges, e.g. `eastus`
- **UAMI Resource ID** — full ARM ID of the puller identity, like `/subscriptions/.../userAssignedIdentities/ctfd-puller`
- **DNS Label Prefix** — used to name container groups and DNS labels, e.g. `ctfd` → `ctfd-abc12345.eastus.azurecontainer.io`
- **ACR Login Server** — e.g. `myregistry.azurecr.io`

### Migrating from upstream / prior installs

This fork changes the plugin's schema (user mode instead of team mode; per-container hostname). If you had the upstream plugin installed before, `db.create_all()` will **not** ALTER existing tables. Drop them before first start so SQLAlchemy can recreate cleanly:

```sql
DROP TABLE IF EXISTS container_info;
DROP TABLE IF EXISTS container_settings;
DROP TABLE IF EXISTS container_challenge_model;
```

Connect to your CTFd database (MariaDB/MySQL or SQLite depending on your setup) and run those. You'll lose existing container-challenge records and admin Settings; reconfigure on the Settings page after restart.

### Notes & caveats

- Provisioning a container group takes ~30-60 seconds; the "Get Connection Info" button shows a `Provisioning…` state while it waits.
- The volumes field on challenges is **ignored** in ACI mode — host-path mounts don't translate to ACI.
- Commands are parsed via `shlex.split`. For complex commands, use `sh -c "your full command line"`.
- The image dropdown is populated by listing repos/tags from the ACR. The CTFd identity needs **AcrPull** on the ACR for this to work.
