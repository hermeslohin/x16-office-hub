# Installing the X16 Office hub with Docker

This guide gets the sync hub running in a container so your Commander X16 can open and save
documents and sync its calendar with your ownCloud server. You need about 20 minutes.

```
X16 (TexElec serial/ESP32 card) --Wi-Fi/TCP 6502--> x16hub container --HTTPS--> ownCloud
```

## What you need

* **A Linux machine on the same LAN as the X16** with Docker Engine and the Compose plugin
  (`docker compose version` works). A Raspberry Pi 4/5 is fine; the image is built for amd64
  and arm64.
  Docker Desktop on macOS or Windows works too, but the hub then sees every connection as
  coming from one Docker address. That's fine for one X16, but repeated failed logins block all
  X16s at once, and the firewall step below is Linux-only.
* **An ownCloud server** reachable over **HTTPS** from that machine (step 1).
* **An X16 with the TexElec X16 Serial & ESP32 Network Card** (Zimodem firmware), on a Wi-Fi
  network that can reach the Linux machine.

## Where everything lives on your machine

Everything goes in one folder in your home directory, `~/x16hub`:

| On your machine | Inside the container | What it is | Who creates it |
|---|---|---|---|
| `~/x16hub/compose.yaml` | | How to run the container | You (step 4) |
| `~/x16hub/.env` | | Your user ID, so file ownership matches | You (step 4) |
| `~/x16hub/secrets/` | `/secrets` | Folder for secrets, mode **700** | You (step 2) |
| `~/x16hub/secrets/owncloud.json` | `/secrets/owncloud.json` | **ownCloud credentials**, mode **600** | You (step 2) |
| `~/x16hub/secrets/ca.pem` | `/secrets/ca.pem` | Certificate that signs your ownCloud's HTTPS cert (only for a private/self-signed cert) | You (step 1) |
| `~/x16hub/secrets/x16link.keys` | `/secrets/x16link.keys` | One secret key per X16 | The hub (step 6) |
| `~/x16hub/data/hub.db` | `/data/hub.db` | Sync state and hub settings | The hub |
| `~/x16hub/sd-staging/X16LINK.KEY` | `/sd/X16LINK.KEY` | Key file to copy to the X16's SD card, then delete | The hub (step 6) |

The hub checks these permissions every time it starts. If `secrets/` is readable by other
users, or the files aren't owned by the user the container runs as, it stops with a message
telling you the `chmod` to run.

---

## Step 1: Prepare ownCloud

### 1a. If you don't have ownCloud yet

Follow ownCloud's official Docker guide:
<https://doc.owncloud.com/server/next/admin_manual/installation/docker/>.
It must be reachable over **`https://`**. The hub won't talk to plain `http://` and always
checks the certificate. If you put ownCloud behind a reverse proxy (Caddy, Traefik, nginx),
the proxy's certificate is the one that counts.

### 1b. Create a user for the hub

Use a dedicated account, so the X16's files and calendar stay separate and its password can
be revoked on its own.

1. Sign in to ownCloud as an admin.
2. Click your name (top right) → **Users**.
3. Create the user **`x16hub`** (any name works; you'll put it in the credentials file).
   Give it a strong password and note it down. The hub never uses this password.

The hub creates what it needs the first time it runs: the **`Documents`** folder (where X16 Write
and X16 Sheets files go) and a calendar named **`X16`**. Install the **Calendar** app from the
ownCloud Market if you want to see that calendar in the web interface. Syncing works without it.

### 1c. Create an app password

The hub signs in with an **app password**, not the account password.

1. Sign out, then sign in as **`x16hub`**.
2. Click the user name (top right) → **Settings** → **Security** (under *Personal*).
3. Under **App passwords / tokens**, enter the name `x16hub` and click **Create new app passcode**.
4. **Copy the password now.** ownCloud shows it only once. You'll paste it in step 2.

### 1d. Find out which certificate to trust

The credentials file has a `ca_file` entry: the certificate the hub uses to check that it is
really talking to your ownCloud.

* **Public certificate** (for example Let's Encrypt, so your browser shows no warning):
  use the container's built-in list: `"ca_file": "/etc/ssl/certs/ca-certificates.crt"`.
  Skip to step 2.
* **Your own CA or a self-signed certificate:** put that CA's certificate (PEM format, starts
  with `-----BEGIN CERTIFICATE-----`) at `~/x16hub/secrets/ca.pem` once you've made that folder
  in step 2, and use `"ca_file": "/secrets/ca.pem"`.
  If the certificate is self-signed and you don't have the file, you can fetch it from the
  server. Then **compare its fingerprint** with the one your browser shows for the site before
  you trust it:

  ```sh
  openssl s_client -connect owncloud.example.lan:443 -servername owncloud.example.lan </dev/null 2>/dev/null \
    | openssl x509 > ~/x16hub/secrets/ca.pem
  openssl x509 -in ~/x16hub/secrets/ca.pem -noout -fingerprint -sha256
  ```

---

## Step 2: Create the folders and the credentials file

```sh
mkdir -p ~/x16hub/secrets ~/x16hub/data ~/x16hub/sd-staging
chmod 700 ~/x16hub/secrets ~/x16hub/data ~/x16hub/sd-staging
```

Now create **`~/x16hub/secrets/owncloud.json`**. Use a text editor so the password doesn't end
up in your shell history:

```sh
nano ~/x16hub/secrets/owncloud.json
```

Paste this and fill in your values:

```json
{
  "base_url": "https://owncloud.example.lan",
  "username": "x16hub",
  "app_password": "paste-the-app-password-from-step-1c",
  "ca_file": "/secrets/ca.pem"
}
```

| Field | What to put |
|---|---|
| `base_url` | The address you open ownCloud at, starting with `https://`. Include a path if ownCloud lives under one (`https://example.lan/owncloud`). No trailing `/index.php`. |
| `username` | The user from step 1b. |
| `app_password` | The app password from step 1c. |
| `ca_file` | The path **inside the container** from step 1d: `/secrets/ca.pem` or `/etc/ssl/certs/ca-certificates.crt`. |

Save, then lock the files down:

```sh
chmod 600 ~/x16hub/secrets/*
```

---

## Step 3: Get the container image

Pick **one** option.

### Option A: Pull the published image (easiest)

```sh
docker pull ghcr.io/hermeslohin/x16-office-hub:latest
```

### Option B: Build it yourself

```sh
git clone https://github.com/hermeslohin/x16-office-hub.git
cd x16-office-hub
docker build --target test .                                        # optional: run the unit tests
docker build -t ghcr.io/hermeslohin/x16-office-hub:latest .          # build the image
```

Tagging your build with the same name means the rest of this guide works unchanged. Compose
uses your local image and only pulls when no image with that name exists.

---

## Step 4: Set up Docker Compose

Download the compose file into `~/x16hub`, or copy `compose.yaml` from your checkout:

```sh
cd ~/x16hub
curl -fsSLo compose.yaml https://raw.githubusercontent.com/hermeslohin/x16-office-hub/main/compose.yaml
```

Tell Compose which user to run as. It must be **you**, the owner of the folders you just made:

```sh
printf 'X16HUB_UID=%s\nX16HUB_GID=%s\n' "$(id -u)" "$(id -g)" > ~/x16hub/.env
```

From here on, run every `docker compose` command from `~/x16hub`.

---

## Step 5: Test the ownCloud connection

```sh
cd ~/x16hub
docker compose run --rm x16hub oc check
```

Every line should say **PASS**. The check uploads, reads back and deletes a test file and a test
calendar event. It leaves behind an empty `X16` folder and the `X16` calendar. If something fails, see
[Troubleshooting](#troubleshooting).

---

## Step 6: Connect the X16

The hub itself needs no address settings: the container listens on port 6502 and the
firewall (step 7) decides who may reach it. The X16 only needs to know where to dial.

### 6a. Create the X16's key file

Each X16 gets its own secret key. This command adds it to `secrets/x16link.keys` and writes the
matching **`X16LINK.KEY`** for the SD card into `~/x16hub/sd-staging/`:

```sh
docker compose run --rm x16hub link key new x16-office --sdcard /sd --host 192.168.1.10:6502
```

* `x16-office` is the device name: up to 16 letters, digits, `-` or `_`.
* `--host` is where the X16 dials: this machine's LAN address plus `:6502`. `hostname -I`
  shows the address (the first one is usually right). A DNS name that your Wi-Fi network
  resolves, such as `hubhost.lan:6502`, works too, and keeps working if the IP changes.
  Give the machine a fixed DHCP lease if you use the IP.

Copy the file to the **root of the X16's SD card**, next to the apps, then delete the staging
copy:

```sh
cp ~/x16hub/sd-staging/X16LINK.KEY /media/$USER/X16/      # adjust to where your card is mounted
shred -u ~/x16hub/sd-staging/X16LINK.KEY
```

Anyone holding that SD card can act as your X16 towards the hub. If a card is lost, revoke its
key with `docker compose run --rm x16hub link key rm x16-office`.

### 6b. Set up the ESP32 (once)

On the X16, open a terminal program on the serial card and enter:

```
ATW"<wifi name>,<wifi password>"     join your Wi-Fi
ATB115200                            115200 baud
AT&K3                                RTS/CTS flow control
AT&W                                 save
```

The apps dial the address in `X16LINK.KEY` themselves.

---

## Step 7: Firewall port 6502

The X16 link is authenticated but not encrypted, so port 6502 should only be reachable from the
network your X16's Wi-Fi is on. Set up a firewall rule on this machine that **allows TCP 6502
from your X16's network (for example `192.168.1.0/24`) and drops it from everywhere else**.

Things to know when you write that rule:

* **ufw, firewalld zones and ordinary `INPUT` rules do not protect Docker ports.** Docker
  forwards published ports before those rules see the traffic. Rules for containers go in
  Docker's **`DOCKER-USER`** chain. Docker's documentation explains how:
  <https://docs.docker.com/engine/network/packet-filtering-firewalls/>.
* In `DOCKER-USER`, match on the port the client dialled (`-m conntrack --ctorigdstport 6502
  --ctdir ORIGINAL`) so the rule only affects new connections to the hub, not its replies or
  its HTTPS connection to ownCloud.
* Make the rule survive reboots and Docker restarts, for example with a small systemd unit
  that runs after `docker.service`.
* If your router has its own firewall, it must let the X16's Wi-Fi network reach this
  machine on TCP 6502.

The hub also has its own allow-list as a second layer. By default it only accepts
private LAN addresses (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`), so you normally don't
need to change it.

---

## Step 8: Start the hub

```sh
cd ~/x16hub
docker compose up -d
docker compose logs -f          # Ctrl+C stops watching; the hub keeps running
```

You should see `x16link listening on ('0.0.0.0', 6502)`. From a machine outside the network your
firewall allows, the port should not answer (`nc -vz -w 3 192.168.1.10 6502` times out). On the X16, open X16 Write and use
**Open** or **Save As**, or press **F1** in the calendar. Each connection shows up in the log.

The container restarts on its own after a crash or a reboot (as long as Docker starts at boot:
`sudo systemctl enable docker`).

---

## Everyday tasks

| Task | Command (in `~/x16hub`) |
|---|---|
| See the log | `docker compose logs -f` |
| Stop / start | `docker compose stop` / `docker compose up -d` |
| Change who may connect | update your firewall rule (step 7) |
| Update to the newest image | `docker compose pull && docker compose up -d` |
| List X16 device names | `docker compose run --rm x16hub link key list` |
| Add another X16 | step 6a with a new device name (no restart needed) |
| Re-test ownCloud | `docker compose run --rm x16hub oc check` |
| Calendar sync status | `docker compose run --rm --entrypoint python x16hub -m x16hub.calsync status` |

### Change the ownCloud app password

1. In ownCloud, as `x16hub`: **Settings → Security → App passwords**. Create a new one.
2. Paste it into `~/x16hub/secrets/owncloud.json` with your editor.
3. `docker compose run --rm x16hub oc check` must pass. Then `docker compose restart`.
4. Back in ownCloud, **revoke the old app password**.

If you think the file has leaked, do this right away. Revoking the old password is what makes it
useless.

### Back up

Back up `~/x16hub/secrets` and `~/x16hub/data` together, and keep the backup private, because
it contains the app password and the device keys. Your documents and calendar themselves live
on ownCloud.

---

## Running without Compose

The same thing with plain `docker run`:

```sh
docker run -d --name x16hub --restart unless-stopped -p 0.0.0.0:6502:6502 \
  --user "$(id -u):$(id -g)" \
  -v ~/x16hub/secrets:/secrets -v ~/x16hub/data:/data -v ~/x16hub/sd-staging:/sd \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  ghcr.io/hermeslohin/x16-office-hub:latest
```

For one-off commands, replace `-d --name x16hub --restart unless-stopped -p 0.0.0.0:6502:6502`
with `--rm` and add the command after the image name, for example
`... x16-office-hub:latest oc check`.

---

## Troubleshooting

| Message or symptom | Fix |
|---|---|
| `... has mode 0o644; run: chmod 600 ...` | `chmod 600 ~/x16hub/secrets/*` |
| `... has mode 0o755; run: chmod 700 ...` | `chmod 700 ~/x16hub/secrets ~/x16hub/data` |
| `... is not owned by the current user; refusing` | The container runs as a different user than the file owner. Check `~/x16hub/.env` matches `id -u` / `id -g`, or fix ownership: `sudo chown -R $(id -u):$(id -g) ~/x16hub`. |
| `/secrets/owncloud.json not found` | The file isn't at `~/x16hub/secrets/owncloud.json`, or you ran `docker compose` outside `~/x16hub`. |
| `... is not valid JSON` | A missing comma or quote. Compare with the example in step 2. |
| `ca_file /secrets/ca.pem not found` | Put the certificate at `~/x16hub/secrets/ca.pem`, or use `/etc/ssl/certs/ca-certificates.crt` for a public certificate. |
| `TLS verification failed` | `ca_file` doesn't match the certificate ownCloud presents. Use the CA that signed it (step 1d), and make sure `base_url` uses the same name as the certificate. |
| `oc check` fails with 401 | Wrong `username` or `app_password`, or the app password was revoked. Create a new one (step 1c). |
| `base_url must be a plain https://host[/path] URL` | Use `https://`, without a user name, `?` or `#` in it. |
| X16 can't connect, nothing in the log | The `HOST=` line in `X16LINK.KEY` must be this machine's current address plus `:6502`. Your firewall rule (step 7) must allow the X16's network; its packet counters show whether attempts are being dropped. Your router's firewall may also block it. |
| Log shows `refused ...: not in allow-list` | The X16's address isn't a private LAN address. Add its network to the hub's list too: `docker compose run --rm x16hub config set allow 192.168.0.0/16,<your network>`, then `docker compose restart`. |
| Log shows `auth failed from ...` | The SD card's `X16LINK.KEY` doesn't match the hub's key (for example after running `key new` again). Redo step 6a and copy the new file. |
| An address gets blocked | 5 failed connections in 10 minutes block that address for 10 minutes. Fix the cause, then wait, or run `docker compose restart`. |
| `warning: no device keys` | No X16 has a key yet. Do step 6a. |

More details on the hub itself (protocol, limits, calendar conflicts) are in
[`hub/README.md`](hub/README.md) and [`PROTOCOL.md`](PROTOCOL.md).
