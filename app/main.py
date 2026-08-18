"""FastAPI application: sign in, convert a PDF, watch it live, download the ZIP."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import auth, config
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

Guarded = Depends(auth.require_session)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    if not config.AUTH_ENABLED and not config.ALLOW_NO_PASSWORD:
        raise RuntimeError(
            "APP_PASSWORD is not set. Set it in your environment (see .env.example), "
            "or set ALLOW_NO_PASSWORD=true if the app is already protected by "
            "Cloudflare Access or a VPN."
        )
    if not config.AUTH_ENABLED:
        log.warning("running without a password - ALLOW_NO_PASSWORD is enabled")

    manager.start()
    restored = manager.restore()
    if restored:
        log.info("restored %d finished archive(s) from disk", restored)

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


app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def api_config(request: Request) -> dict:
    """Public: the login screen needs this before a session exists."""
    return {
        "app_name": config.APP_NAME,
        "auth_enabled": config.AUTH_ENABLED,
        "authenticated": auth.is_authenticated(request),
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "max_pages": config.MAX_PAGES,
        "default_dpi": config.DEFAULT_DPI,
        "min_dpi": config.MIN_DPI,
        "max_dpi": config.MAX_DPI,
        "render_workers": config.RENDER_WORKERS,
        "job_ttl_minutes": config.JOB_TTL_MINUTES,
    }


@app.post("/api/login")
async def login(response: Response, request: Request, password: str = Form("")) -> dict:
    if not config.AUTH_ENABLED:
        return {"authenticated": True}

    client = auth.client_key(request)
    locked = auth.limiter.locked_for(client)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {locked // 60 + 1} minute(s).",
        )

    if not auth.password_matches(password):
        auth.limiter.record_failure(client)
        log.warning("failed login from %s", client)
        raise HTTPException(status_code=401, detail="Wrong password.")

    auth.limiter.clear(client)
    response.set_cookie(value=auth.issue_token(), **auth.cookie_kwargs())
    log.info("login from %s", client)
    return {"authenticated": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"authenticated": False}


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
@app.post("/api/upload", dependencies=[Guarded])
async def upload(
    file: UploadFile,
    dpi: int = Form(config.DEFAULT_DPI),
    color_mode: str = Form("color"),
    compression: str = Form("lzw"),
    zip_compress: str = Form("false"),
    auto_delete: str = Form("false"),
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
        zip_compress=_truthy(zip_compress),
        auto_delete=_truthy(auto_delete),
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


@app.get("/api/jobs", dependencies=[Guarded])
async def list_jobs() -> dict:
    """Everything this server still knows about - lets any browser pick up
    a conversion that is already running, or re-download a finished one."""
    return {"jobs": manager.list_jobs()}


@app.get("/api/status/{job_id}", dependencies=[Guarded])
async def status(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job.")
    return job.snapshot()


@app.post("/api/cancel/{job_id}", dependencies=[Guarded])
async def cancel(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=404, detail="Job is not running.")
    return {"cancelled": True}


@app.delete("/api/jobs/{job_id}", dependencies=[Guarded])
async def delete_job(job_id: str) -> dict:
    if not manager.delete(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired job.")
    return {"deleted": True}


@app.get("/api/events/{job_id}", dependencies=[Guarded])
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


@app.get("/api/download/{job_id}", dependencies=[Guarded])
async def download(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job.")
    if job.stage != "done":
        raise HTTPException(status_code=409, detail="This job is not finished yet.")

    zip_path = job.dir / job.zip_name
    if not zip_path.is_file():
        raise HTTPException(status_code=410, detail="The archive has already expired.")

    manager.touch_download(job)
    # "Delete after download" runs once the response has been fully sent.
    cleanup = BackgroundTask(manager.delete, job.id) if job.auto_delete else None

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=job.zip_name,
        headers={"Cache-Control": "no-store"},
        background=cleanup,
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.middleware("http")
async def no_store_app_shell(request: Request, call_next):
    """Keep index.html / app.js / style.css out of every cache.

    Cloudflare caches .js and .css by file extension but leaves the HTML
    alone, so after a redeploy a browser could pair new markup with a stale
    script - which renders a blank page. The app is tiny and private, so
    revalidating on every load costs nothing and removes the whole class of
    problem. API responses set their own headers.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
