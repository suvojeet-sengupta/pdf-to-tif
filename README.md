# PDF → TIFF

A small self-hosted web app: upload a PDF, every page is rendered to its own TIFF,
and the whole set comes back as a single ZIP. Progress is streamed live to the
browser — page counter, percentage, speed and ETA — and the download link appears
as soon as the archive is built.

Built for speed: pages render in parallel across a pool of worker processes, TIFFs
use fast lossless compression (LZW by default), and the ZIP is **stored, not
deflated**, so packaging is effectively instant.

![port](https://img.shields.io/badge/port-9000-4f8cff) ![docker](https://img.shields.io/badge/docker-ready-2496ed)

---

## Features

- Drag-and-drop upload with real upload progress
- One TIFF per PDF page, named `<document>_page_0001.tif`, `…_0002.tif`, …
- Live conversion progress over Server-Sent Events (pages done, %, pages/sec, ETA, activity log)
- ZIP built with no compression by default → fast, since TIFFs are already compressed
- Resolution 100–600 DPI, colour / grayscale / bilevel (CCITT G4)
- Cancel a running job; finished archives auto-delete after a TTL
- Runs as one container on port **9000**, ready for a Cloudflare Tunnel
- No external services — everything happens on your own box

---

## Quick start (Docker)

```bash
git clone https://github.com/suvojeet321/pdf-to-tif.git
cd pdf-to-tif
docker compose up -d --build
```

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

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/upload` | multipart: `file`, `dpi`, `color_mode`, `compression`, `zip_compress` → `{job_id}` |
| `GET` | `/api/events/{job_id}` | SSE stream of progress snapshots |
| `GET` | `/api/status/{job_id}` | One-shot progress snapshot (polling fallback) |
| `POST` | `/api/cancel/{job_id}` | Cancel a running job |
| `GET` | `/api/download/{job_id}` | Download the finished ZIP |
| `GET` | `/api/config` | Limits the UI reads at boot |
| `GET` | `/healthz` | Health probe |

Convert from the command line:

```bash
JOB=$(curl -s -F file=@input.pdf -F dpi=200 -F color_mode=color \
      http://localhost:9000/api/upload | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -N http://localhost:9000/api/events/$JOB        # watch progress
curl -OJ http://localhost:9000/api/download/$JOB     # grab the archive
```

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload --port 9000
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
```

Job state lives in memory, so the container runs a single uvicorn worker; the
CPU-heavy work is what fans out across processes.

## Licence

MIT
