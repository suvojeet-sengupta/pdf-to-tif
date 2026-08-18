"""FastAPI application: upload a PDF, watch it convert, download the ZIP."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .converter import normalise_options
from .jobs import manager

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("pdf2tif")

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 1024 * 1024

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Stops nginx / Cloudflare style proxies from buffering the event stream.
    "X-Accel-Buffering": "no",
}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    manager.start()
    cleaner = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleaner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleaner
        manager.shutdown()


async def _cleanup_loop() -> None:
    while True:
        try:
            removed = await asyncio.to_thread(manager.cleanup)
            if removed:
                log.info("cleaned up %d expired job(s)", removed)
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)


app = FastAPI(title="PDF to TIFF", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/api/config")
async def api_config() -> dict:
    return {
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "max_pages": config.MAX_PAGES,
        "default_dpi": config.DEFAULT_DPI,
        "min_dpi": config.MIN_DPI,
        "max_dpi": config.MAX_DPI,
        "render_workers": config.RENDER_WORKERS,
        "job_ttl_minutes": config.JOB_TTL_MINUTES,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload(
    file: UploadFile,
    dpi: int = Form(config.DEFAULT_DPI),
    color_mode: str = Form("color"),
    compression: str = Form("lzw"),
    zip_compress: str = Form("false"),
) -> JSONResponse:
    filename = (file.filename or "document.pdf").strip()
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf file.")

    dpi, color_mode, compression = normalise_options(
        dpi, color_mode, compression, config.MIN_DPI, config.MAX_DPI, config.DEFAULT_DPI
    )

    job = manager.create(
        original_name=filename,
        dpi=dpi,
        color_mode=color_mode,
        compression=compression,
        zip_compress=str(zip_compress).lower() in ("1", "true", "yes", "on"),
    )

    written = 0
    try:
        with open(job.pdf_path, "wb") as handle:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than the {config.MAX_UPLOAD_MB} MB limit.",
                    )
                handle.write(chunk)
    except HTTPException:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(job.dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        await file.close()

    if written == 0:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    manager.submit(job)
    log.info("job %s accepted (%s, %.1f MB)", job.id, filename, written / 1024 / 1024)
    return JSONResponse({"job_id": job.id, "status_url": f"/api/events/{job.id}"})


@app.get("/api/status/{job_id}")
async def status(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job.")
    return job.snapshot()


@app.post("/api/cancel/{job_id}")
async def cancel(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=404, detail="Job is not running.")
    return {"cancelled": True}


@app.get("/api/events/{job_id}")
async def events(job_id: str, request: Request) -> StreamingResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job.")

    async def stream():
        last_version = -1
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                return
            version = job.version
            if version != last_version:
                last_version = version
                idle_ticks = 0
                yield f"data: {json.dumps(job.snapshot())}\n\n"
                if job.terminal:
                    return
            else:
                idle_ticks += 1
                if idle_ticks >= 60:  # ~15s keepalive through the tunnel
                    idle_ticks = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job.")
    if job.stage != "done":
        raise HTTPException(status_code=409, detail="This job is not finished yet.")

    zip_path = job.dir / job.zip_name
    if not zip_path.is_file():
        raise HTTPException(status_code=410, detail="The archive has already expired.")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=job.zip_name,
        headers={"Cache-Control": "no-store"},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
