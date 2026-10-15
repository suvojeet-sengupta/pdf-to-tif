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
- **Chunked resumable uploads** — the file is sent a few megabytes at a time; if a
  chunk fails only that chunk is retried (with exponential backoff), and reloading the
  page resumes from the server's last confirmed byte instead of starting over
- One TIFF per PDF page, named `<document>_page_001.tif`, `…_002.tif`, …
- Live progress over Server-Sent Events: page mosaic, %, pages/sec, ETA, activity log
- **Server-side jobs** — reload the page or open it on your phone and the running conversion is still there
- **Library** of everything still on the server, with re-download and one-click delete
- **Delete after download** — tick the box and the archive is wiped the moment the download finishes
- **Streaming ZIP** — each page is added to the archive and deleted from disk as soon
  as it renders, so peak disk usage stays flat even for 10 000-page documents
- ZIP stored rather than deflated → fast, since TIFFs are already compressed
- Resolution 100–600 DPI, colour / grayscale / bilevel (CCITT G4)
- **Large-document safety rails** — sliding render window, per-page pixel cap, periodic
  disk-space checks, and a configurable page-count ceiling
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
- Free plans cap uploads at **100 MB** per request. The chunked uploader sends
  4 MB chunks by default, which stays well under this cap — even 500 MB PDFs
  work fine on the free plan.

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
| `MAX_CONCURRENT_JOBS` | `1` | How many PDFs may convert at the same time (1 = a batch converts one by one) |
| `MAX_BATCH_FILES` | `5` | PDFs the UI lets you pick and queue in one go |
| `RENDER_MEMORY_MB` | 80% of the container limit | Estimated memory all page renders may hold at once; large sheets are dispatched only while they fit |
| `MAX_UPLOAD_MB` | `500` | Rejected above this size |
| `MAX_PAGES` | `10000` | Page-count ceiling per PDF |
| `JOB_TTL_MINUTES` | `120` | How long a finished ZIP stays downloadable |
| `DEFAULT_DPI` | `200` | Resolution preselected in the UI |
| `MAX_DPI` | `600` | Highest resolution a user may pick |
| `DATA_DIR` | `/data` | Where jobs and archives are written |
| `UPLOAD_CHUNK_MB` | `4` | Chunk size for resumable uploads (MB) |
| `UPLOAD_TTL_MINUTES` | `180` | Abandoned uploads are cleaned up after this |
| `INFLIGHT_PER_WORKER` | `3` | Render tasks queued per worker (sliding window) |
| `MIN_FREE_DISK_MB` | `1024` | Abort a job if free disk drops below this |
| `DISK_CHECK_EVERY` | `25` | Check free disk space every N pages |
| `MAX_PAGE_PIXELS` | `120000000` | Scale down pages exceeding this pixel count |

### Tuning

- **Faster**: lower the DPI (200 is plenty for documents), pick *Black & white (G4)*
  for scans, and raise `RENDER_WORKERS` to your core count.
- **Smaller output**: *Deflate* TIFF compression, or *Black & white* which is
  dramatically smaller than colour.
- **Largest / fastest per page**: *PackBits*, or *None* for raw uncompressed TIFF.
- **Large documents**: the render window (`INFLIGHT_PER_WORKER × RENDER_WORKERS`)
  keeps memory flat. Raise `INFLIGHT_PER_WORKER` for higher throughput on fast disks,
  lower it on memory-constrained boxes.

---

## API

Every endpoint below except `/api/config`, `/api/login` and `/healthz` requires
the session cookie.

### Single-request upload (legacy)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/upload` | multipart: `file`, `dpi`, `color_mode`, `compression`, `zip_compress`, `auto_delete` → `{job_id}` |

### Chunked resumable upload

The browser sends the file a few megabytes at a time. If a chunk fails, only
that chunk is retried (with exponential backoff). A page reload resumes from
the server's confirmed byte offset instead of starting over.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/uploads` | JSON body: `{filename, size, dpi, color_mode, compression, zip_compress, auto_delete}` → `{upload_id, chunk_size, received}` |
| `GET` | `/api/uploads/{id}` | Current byte offset — the client resumes from here |
| `PATCH` | `/api/uploads/{id}` | Send one chunk as `application/octet-stream`, header `X-Offset: <n>` → `{received}` |
| `POST` | `/api/uploads/{id}/finish` | Promote a complete upload to a conversion job → `{job_id}` |
| `DELETE` | `/api/uploads/{id}` | Discard an incomplete upload |

### Jobs and events

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/jobs` | Everything the server still holds (running + finished) |
| `DELETE` | `/api/jobs/{job_id}` | Delete a job and its archive from the server |
| `GET` | `/api/events/{job_id}` | SSE stream of progress snapshots |
| `GET` | `/api/status/{job_id}` | One-shot progress snapshot (polling fallback) |
| `POST` | `/api/cancel/{job_id}` | Cancel a running job |
| `GET` | `/api/download/{job_id}` | Download the finished ZIP |

### Other

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/login` | form field `password` → sets the session cookie |
| `POST` | `/api/logout` | clears the session cookie |
| `GET` | `/api/config` | Limits the UI reads at boot |
| `GET` | `/healthz` | Health probe |

### Command-line example

```bash
# Log in
curl -s -c jar -F password='your-password' http://localhost:9000/api/login

# Simple upload (small files)
JOB=$(curl -s -b jar -F file=@input.pdf -F dpi=200 -F color_mode=color \
      http://localhost:9000/api/upload | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# Chunked upload (large files)
UPLOAD=$(curl -s -b jar -X POST \
  -H 'Content-Type: application/json' \
  -d '{"filename":"input.pdf","size":'$(stat -c%s input.pdf)'}' \
  http://localhost:9000/api/uploads | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["upload_id"],d["chunk_size"])')
# ... then send chunks with PATCH and finish with POST /api/uploads/{id}/finish

# Watch + download
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
browser slices file into 4 MB chunks
  │
  ├─▶ POST /api/uploads          → server creates session, returns upload_id
  │
  ├─▶ PATCH /api/uploads/{id}    → each chunk appended at X-Offset
  │     ↓ failure?  retry that chunk (exponential backoff)
  │     ↓ reload?   GET /api/uploads/{id} → resume from server's offset
  │
  └─▶ POST /api/uploads/{id}/finish  → all bytes arrived, start conversion
       │
       ├─▶ PyMuPDF opens the PDF, page count is read
       │
       ├─▶ ProcessPoolExecutor renders N pages at once (sliding window)
       │     page → pixmap → Pillow → page_0001.tif
       │     each finished page → added to ZIP, deleted from disk
       │     progress pushed to browsers via SSE
       │
       └─▶ download link, deleted after JOB_TTL_MINUTES
             or immediately, if "delete after download" is on
```

Job state lives in memory, so the container runs a single uvicorn worker; the
CPU-heavy work is what fans out across processes. Finished jobs also write a
small `meta.json` next to their archive, which is how the library is rebuilt
after a restart. Browsers hold no job state at all — they ask `/api/jobs` on
load, so any device with the password sees the same conversions.

Pages are added to the ZIP archive and deleted from disk as each one finishes
rendering, so peak disk usage stays at roughly one copy of the output rather
than two — and there is no long silent "packaging" phase at the end.

## Licence

MIT
