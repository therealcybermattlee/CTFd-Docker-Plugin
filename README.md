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

## Deployment on Linux with Docker Compose

This recipe matches a common self-hosted layout: a top-level directory holding `compose.yaml`, the CTFd source tree, and a Caddy reverse-proxy directory side-by-side:

```
~/ctfd-stack/
├── compose.yaml
├── CTFd/                  ← cloned from github.com/CTFd/CTFd
│   ├── Dockerfile         ← upstream
│   ├── Dockerfile.ctfd    ← you'll add this
│   └── CTFd/
│       └── plugins/
│           └── containers/  ← this plugin lives here
└── caddy/
    ├── Caddyfile
    └── data/, config/
```

Adapt to your own paths. Docker Engine 24+ with the `docker compose` v2 plugin is assumed.

### 1. Clone CTFd and the plugin

The plugin directory **must** be named exactly `containers` — that's where the URL prefix `/containers` is registered.

```bash
cd ~/ctfd-stack

git clone https://github.com/CTFd/CTFd.git
git clone https://github.com/therealcybermattlee/CTFd-Docker-Plugin.git \
  CTFd/CTFd/plugins/containers
```

After this you should have `~/ctfd-stack/CTFd/CTFd/plugins/containers/__init__.py`.

### 2. Custom Dockerfile that installs the plugin's Python deps

The stock `ctfd/ctfd` image doesn't include `azure-identity`, `azure-mgmt-containerinstance`, or `azure-containerregistry`. Create `~/ctfd-stack/CTFd/Dockerfile.ctfd`:

```dockerfile
FROM ctfd/ctfd:latest
USER root
COPY CTFd/plugins/containers/requirements.txt /tmp/plugin-requirements.txt
RUN pip install --no-cache-dir -r /tmp/plugin-requirements.txt
USER 1001
```

The build context will be the `CTFd/` directory, so the `COPY` path is relative to that.

### 3. Wire it into `compose.yaml`

The typical setup has two networks — `ctfd_frontend` (Caddy + CTFd, internet-egress) and `ctfd_backend` (DB + Redis, marked `internal: true` so the DB isn't reachable from the host network). **The CTFd service must attach to both** so it can reach the DB *and* reach `management.azure.com` for the ACI calls.

**Replace** the pinned `image:` line in your existing `ctfd` service with a `build:` block (keep everything else — env vars, volumes, depends_on, networks). Before:

```yaml
  ctfd:
    image: ctfd/ctfd:3.8.4
    # ... your existing env / volumes / depends_on / networks ...
```

After:

```yaml
  ctfd:
    build:
      context: ./CTFd
      dockerfile: Dockerfile.ctfd
    image: ctfd-with-containers:latest   # tag for the locally-built image
    restart: unless-stopped
    depends_on:
      - db
      - cache
    networks:
      - ctfd_frontend
      - ctfd_backend
    environment:
      # --- Existing CTFd env vars stay as-is ---
      UPLOAD_FOLDER: /var/uploads
      LOG_FOLDER: /var/log/CTFd
      DATABASE_URL: mysql+pymysql://ctfd:${MARIADB_PASSWORD}@db/ctfd
      REDIS_URL: redis://cache:6379
      WORKERS: "4"
      ACCESS_LOG: "-"
      ERROR_LOG: "-"
      REVERSE_PROXY: "true"   # trust X-Forwarded-* from Caddy

      # --- Azure SDK auth — pick ONE of the two options below ---
      #
      # Option A (recommended when CTFd runs on an Azure VM): leave these unset and assign
      # the VM a system-assigned managed identity with Contributor on the challenge RG +
      # AcrPull on the ACR. DefaultAzureCredential picks up the VM's IMDS endpoint
      # (169.254.169.254) automatically — no secrets in env vars.
      #
      # Option B (CTFd not on Azure, or you prefer an explicit service principal):
      # AZURE_TENANT_ID: ${AZURE_TENANT_ID}
      # AZURE_CLIENT_ID: ${AZURE_CLIENT_ID}
      # AZURE_CLIENT_SECRET: ${AZURE_CLIENT_SECRET}
    volumes:
      - ctfd_logs:/var/log/CTFd
      - ctfd_uploads:/var/uploads
      - ./CTFd/.ctfd_secret_key:/opt/CTFd/.ctfd_secret_key:ro
    # --- For the Docker backend (challenges run on this host), also mount the host's Docker socket ---
    # volumes:
    #   - /var/run/docker.sock:/var/run/docker.sock
```

**Important:** the `.ctfd_secret_key` file mount must exist as a regular file on the host before `docker compose up`. If it doesn't, Docker creates an empty *directory* there and CTFd fails to start. Pre-create it once and let CTFd populate it on first boot:

```bash
touch ~/ctfd-stack/CTFd/.ctfd_secret_key
chmod 600 ~/ctfd-stack/CTFd/.ctfd_secret_key
```

If you go with **Option A (managed identity on the VM)** — which is the natural fit when CTFd runs on an Azure VM:

```bash
# Enable system-assigned MI on the CTFd host VM (run on your workstation or in Cloud Shell)
az vm identity assign -g <vm-rg> -n <vm-name>

# Grab the principalId it printed and grant the roles it needs
PRINCIPAL_ID=$(az vm show -g <vm-rg> -n <vm-name> --query identity.principalId -o tsv)
az role assignment create --assignee "$PRINCIPAL_ID" \
  --role "Contributor" \
  --scope "/subscriptions/<sub>/resourceGroups/<challenge-rg>"
az role assignment create --assignee "$PRINCIPAL_ID" \
  --role "AcrPull" \
  --scope "/subscriptions/<sub>/resourceGroups/<acr-rg>/providers/Microsoft.ContainerRegistry/registries/<acr-name>"
```

If you go with **Option B (service principal env vars)**, drop the values in `~/ctfd-stack/.env` next to `compose.yaml`:

```
AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000
AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000
AZURE_CLIENT_SECRET=...
```

Lock it down: `chmod 600 .env` and add `.env` to `.gitignore`.

### 4. Caddy in front of CTFd

If you're using `ghcr.io/caddybuilds/caddy-cloudflare:2-alpine` (Caddy with Cloudflare DNS-01 ACME) or any other Caddy build, **no changes are required for this plugin**. A typical `caddy/Caddyfile` is just:

```
your-ctfd.example.com {
    reverse_proxy ctfd:8000
}
```

(plus your `tls { dns cloudflare {env.CF_API_TOKEN} }` block if you're using DNS-01.)

The CTFd service listens on port 8000 inside its container; expose only Caddy on `:443`/`:80` to the public. With the async refactor in this plugin, `POST /containers/api/request` returns in under a second (HTTP 202) and `GET /containers/api/status/<id>` is also fast — so the default Caddy and Cloudflare timeouts are fine. **No timeout bumps required**, including behind Cloudflare's 100s edge limit.

### 5. Build and start

```bash
cd ~/ctfd-stack
docker compose build ctfd
docker compose up -d
```

Confirm the plugin loaded:

```bash
docker compose logs -f ctfd | head -50
```

You shouldn't see `ImportError` or `ModuleNotFoundError`. Hit `https://your-ctfd.example.com/admin` and look for **Plugins → Containers** in the navbar dropdown.

### 6. If you previously had the upstream plugin installed, drop its tables

This fork's schema is incompatible with upstream (user mode instead of team mode, `hostname` column, async status columns, unique constraint on `(challenge_id, user_id)`). On a brand-new database this step is a no-op. On an upgrade, drop the tables once so SQLAlchemy can recreate them on next start:

```bash
# MariaDB / MySQL — service name and creds must match your compose.yaml
docker compose exec db mariadb -uctfd -pctfd ctfd \
  -e "DROP TABLE IF EXISTS container_info; DROP TABLE IF EXISTS container_settings;"
```

Restart CTFd: `docker compose restart ctfd`.

### 7. Configure via the admin UI

1. Browse to `https://your-ctfd.example.com/admin` and log in.
2. **Plugins → Containers → Settings**.
3. Set **Backend** to **Azure Container Instances** and fill the Azure fields (Subscription ID, Resource Group, Region, UAMI Resource ID, ACR Login Server, DNS prefix). See [Plugin settings](#plugin-settings) below for what each field means.
4. Save. The dashboard badge flips to **Docker Connected** (green) — the label still says "Docker" but it's the same connection check.
5. **Admin → Challenges → New Challenge → container** to create your first challenge. The Image dropdown is populated from your ACR (CTFd's identity needs `AcrPull`).

### 8. Verify end-to-end

As a normal player (not admin), open the challenge and click **Get Connection Info**. You should see `Provisioning…` for ~30-60s, then a `hostname:port` line. From the host shell, confirm the container group exists:

```bash
az container list -g <your-rg> -o table
```

Click **Stop** in the UI and watch the container group disappear within ~30s.

### Updating the plugin later

```bash
cd ~/ctfd-stack/CTFd/CTFd/plugins/containers
git pull
cd ~/ctfd-stack
docker compose build ctfd && docker compose up -d ctfd
```

If a future update introduces new schema columns, you'll see startup errors mentioning unknown columns — drop the affected tables (step 6) and CTFd recreates them.

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

- Provisioning a container group takes ~30-60 seconds. The "Get Connection Info" button shows a `Provisioning…` state while the frontend polls `/containers/api/status/<id>` every 3 seconds. The original POST returns immediately with HTTP 202, so this works behind reverse proxies that have short response timeouts (default Gunicorn 30s, default nginx 60s, Cloudflare free tier 100s).
- The volumes field on challenges is **ignored** in ACI mode — host-path mounts don't translate to ACI.
- Commands are parsed via `shlex.split`. For complex commands, use `sh -c "your full command line"`.
- The image dropdown is populated by listing repos/tags from the ACR. The CTFd identity needs **AcrPull** on the ACR for this to work.
