# Pagesmith — PDF → TIFF

A small self-hosted web app: upload a PDF, every page is rendered to its own TIFF,
and the whole set comes back as a single ZIP. Progress is streamed live to the
browser — page counter, percentage, speed and ETA — and the download link appears
as soon as the archive is built.

Built for speed: pages render in parallel across a pool of worker processes, TIFFs
use fast lossless compression (LZW by default), and the ZIP is **stored, not
deflated**, so packaging is effectively instant.

The whole app sits behind a single shared password, and conversions live on the
server rather than in the tab — close the browser mid-job, come back later, and
you land straight back on the running progress view.

![port](https://img.shields.io/badge/port-9000-4f8cff) ![docker](https://img.shields.io/badge/docker-ready-2496ed)

---

## Features

- **Password protected** — one shared password, signed session cookie, per-IP lockout after repeated failures
- Drag-and-drop upload with real upload progress
- One TIFF per PDF page, named `<document>_page_001.tif`, `…_002.tif`, …
- Live progress over Server-Sent Events: page mosaic, %, pages/sec, ETA, activity log
- **Server-side jobs** — reload the page or open it on your phone and the running conversion is still there
- **Library** of everything still on the server, with re-download and one-click delete
- **Delete after download** — tick the box and the archive is wiped the moment the download finishes
- ZIP stored rather than deflated → fast, since TIFFs are already compressed
- Resolution 100–600 DPI, colour / grayscale / bilevel (CCITT G4)
- Cancel a running job; finished archives expire automatically
- Finished archives survive a container restart
- Responsive UI built for phone and desktop, light and dark
- Runs as one container on port **9000**, ready for a Cloudflare Tunnel
- No external services — everything happens on your own box

---

## Quick start (Docker)

```bash
git clone https://github.com/suvojeet-sengupta/pdf-to-tif.git
cd pdf-to-tif

cp .env.example .env
nano .env                       # set APP_PASSWORD to whatever you want to log in with

# SESSION_SECRET keeps you logged in across restarts - generate one:
python3 -c 'import secrets; print(secrets.token_hex(32))'
# ...and paste it into SESSION_SECRET= in .env

docker compose up -d --build
```

> The app refuses to start without `APP_PASSWORD`. If the instance is already
> protected by Cloudflare Access or a VPN, set `ALLOW_NO_PASSWORD=true` instead.
>
> `.env` is git-ignored on purpose — it holds your password, so it is never
> committed. Create it fresh on every machine you deploy to.

Then open <http://localhost:9000>.

Check it is healthy:

```bash
docker compose ps
curl -s http://localhost:9000/healthz
```

Logs and lifecycle:

```bash
docker compose logs -f
docker compose restart
docker compose down          # stop (keeps the data volume)
docker compose down -v       # stop and wipe stored archives
```

### Without compose

```bash
docker build -t pdf-to-tif .
docker run -d --name pdf-to-tif --restart unless-stopped \
  -e APP_PASSWORD='your-password' \
  -e SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
  -p 127.0.0.1:9000:9000 -v pdf2tif-data:/data pdf-to-tif
```

---

## Cloudflare Tunnel

The compose file publishes the port on `127.0.0.1` only, so the app is not
reachable from the internet directly — `cloudflared` on the host fronts it.

Install and authenticate once:

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared

cloudflared tunnel login
cloudflared tunnel create pdf-to-tif
cloudflared tunnel route dns pdf-to-tif pdf.example.com   # your own domain
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: pdf-to-tif
credentials-file: /root/.cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: pdf.example.com
    service: http://localhost:9000
    originRequest:
      # Big uploads and long conversions must not be cut off mid-flight.
      connectTimeout: 30s
      noHappyEyeballs: true
      # Server-Sent Events need an unbuffered, long-lived connection.
      disableChunkedEncoding: false
  - service: http_status:404
```

Run it as a service:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

Two things worth setting in the Cloudflare dashboard for that hostname:

- **Speed → Optimization**: leave Rocket Loader **off** (it can break the SSE client).
- Free plans cap uploads at **100 MB** per request. If you need larger PDFs,
  either use a paid plan or point the tunnel at the app and upload over the LAN.

---

## Configuration

All settings are environment variables — set them in `docker-compose.yml`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_PASSWORD` | — | **Required.** Shared password for the web UI |
| `ALLOW_NO_PASSWORD` | `false` | Start with no password (only behind Access / VPN) |
| `SESSION_SECRET` | random | Signs session cookies; set it so logins survive restarts |
| `SESSION_TTL_HOURS` | `336` | How long a login stays valid |
| `COOKIE_SECURE` | `true` | Set `false` only if you browse over plain `http://` |
| `LOGIN_MAX_ATTEMPTS` | `8` | Failed logins before a lockout |
| `LOGIN_LOCKOUT_SECONDS` | `300` | Lockout duration |
| `APP_NAME` | `Pagesmith` | Name shown in the UI |
| `LIBRARY_LIMIT` | `40` | Jobs listed in the library |
| `PORT` | `9000` | HTTP port inside the container |
| `RENDER_WORKERS` | `min(8, cores)` | Worker processes rendering pages in parallel |
| `MAX_CONCURRENT_JOBS` | `2` | How many PDFs may convert at the same time |
| `MAX_UPLOAD_MB` | `500` | Rejected above this size |
| `MAX_PAGES` | `3000` | Page-count ceiling per PDF |
| `JOB_TTL_MINUTES` | `120` | How long a finished ZIP stays downloadable |
| `DEFAULT_DPI` | `200` | Resolution preselected in the UI |
| `MAX_DPI` | `600` | Highest resolution a user may pick |
| `DATA_DIR` | `/data` | Where jobs and archives are written |

### Tuning

- **Faster**: lower the DPI (200 is plenty for documents), pick *Black & white (G4)*
  for scans, and raise `RENDER_WORKERS` to your core count.
- **Smaller output**: *Deflate* TIFF compression, or *Black & white* which is
  dramatically smaller than colour.
- **Largest / fastest per page**: *PackBits*, or *None* for raw uncompressed TIFF.

---

## API

Every endpoint below except `/api/config`, `/api/login` and `/healthz` requires
the session cookie.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/login` | form field `password` → sets the session cookie |
| `POST` | `/api/logout` | clears the session cookie |
| `POST` | `/api/upload` | multipart: `file`, `dpi`, `color_mode`, `compression`, `zip_compress`, `auto_delete` → `{job_id}` |
| `GET` | `/api/jobs` | Everything the server still holds (running + finished) |
| `DELETE` | `/api/jobs/{job_id}` | Delete a job and its archive from the server |
| `GET` | `/api/events/{job_id}` | SSE stream of progress snapshots |
| `GET` | `/api/status/{job_id}` | One-shot progress snapshot (polling fallback) |
| `POST` | `/api/cancel/{job_id}` | Cancel a running job |
| `GET` | `/api/download/{job_id}` | Download the finished ZIP |
| `GET` | `/api/config` | Limits the UI reads at boot |
| `GET` | `/healthz` | Health probe |

Convert from the command line:

```bash
curl -s -c jar -F password='your-password' http://localhost:9000/api/login

JOB=$(curl -s -b jar -F file=@input.pdf -F dpi=200 -F color_mode=color \
      http://localhost:9000/api/upload | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -N  -b jar http://localhost:9000/api/events/$JOB        # watch progress
curl -OJ -b jar http://localhost:9000/api/download/$JOB      # grab the archive
curl -X DELETE -b jar http://localhost:9000/api/jobs/$JOB    # wipe it from the server
```

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data APP_PASSWORD=dev COOKIE_SECURE=false \
  uvicorn app.main:app --reload --port 9000
```

## How it works

```
upload  ──▶  PyMuPDF opens the PDF, page count is read
             │
             ├─▶ ProcessPoolExecutor renders N pages at once
             │     page → pixmap → Pillow → page_0001.tif
             │     each finished page bumps the live counter
             │
             └─▶ zipfile (ZIP_STORED) packs the TIFFs
                   │
                   └─▶ download link, deleted after JOB_TTL_MINUTES
                       or immediately, if "delete after download" is on
```

Job state lives in memory, so the container runs a single uvicorn worker; the
CPU-heavy work is what fans out across processes. Finished jobs also write a
small `meta.json` next to their archive, which is how the library is rebuilt
after a restart. Browsers hold no job state at all — they ask `/api/jobs` on
load, so any device with the password sees the same conversions.

## Licence

MIT
