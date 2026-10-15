"""Job store and the render -> zip pipeline."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .converter import RenderSpec, estimate_render_bytes, inspect_pdf, render_page

log = logging.getLogger("pdf2tif.jobs")

STAGE_LABELS = {
    "queued": "Waiting in queue",
    "analyzing": "Reading PDF",
    "rendering": "Converting pages to TIFF",
    "packaging": "Building ZIP archive",
    "done": "Finished",
    "error": "Failed",
    "cancelled": "Cancelled",
}


class JobCancelled(Exception):
    pass


class MemoryBudget:
    """Shared ceiling on the estimated memory of pages being rendered."""

    def __init__(self, total: int) -> None:
        self.total = max(1, total)
        self._used = 0
        self._lock = threading.Lock()

    def try_acquire(self, amount: int) -> int:
        # A page bigger than the whole budget still runs, just on its own.
        amount = min(amount, self.total)
        with self._lock:
            if self._used + amount > self.total:
                return 0
            self._used += amount
            return amount

    def release(self, amount: int) -> None:
        with self._lock:
            self._used = max(0, self._used - amount)


@dataclass
class Job:
    id: str
    original_name: str
    dpi: int
    color_mode: str
    compression: str
    zip_compress: bool
    dir: Path
    pdf_path: Path

    auto_delete: bool = False
    stage: str = "queued"
    total_pages: int = 0
    done_pages: int = 0
    bytes_written: int = 0
    zip_name: str = ""
    zip_size: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_requested: bool = False
    downloads: int = 0
    log_lines: list[str] = field(default_factory=list)
    page_sizes: list[tuple[float, float]] = field(default_factory=list, repr=False)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _version: int = 0

    # -- state helpers -----------------------------------------------------
    def update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
            self._version += 1

    def note(self, message: str) -> None:
        with self._lock:
            self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {message}")
            del self.log_lines[:-200]
            self._version += 1

    def bump_page(self, written: int) -> tuple[int, int]:
        with self._lock:
            self.done_pages += 1
            self.bytes_written += written
            self._version += 1
            return self.done_pages, self.bytes_written

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def terminal(self) -> bool:
        return self.stage in ("done", "error", "cancelled")

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (self.finished_at or time.time()) - (
                self.started_at or self.created_at
            )
            if self.total_pages:
                percent = round(self.done_pages / self.total_pages * 100, 1)
            else:
                percent = 0.0
            if self.stage in ("packaging", "done"):
                percent = 100.0 if self.stage == "done" else max(percent, 99.0)

            rate = self.done_pages / elapsed if elapsed > 0.5 and self.done_pages else 0.0
            remaining = max(0, self.total_pages - self.done_pages)
            eta = remaining / rate if rate > 0 and self.stage == "rendering" else 0.0

            return {
                "id": self.id,
                "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "original_name": self.original_name,
                "total_pages": self.total_pages,
                "done_pages": self.done_pages,
                "percent": percent,
                "bytes_written": self.bytes_written,
                "zip_name": self.zip_name,
                "zip_size": self.zip_size,
                "error": self.error,
                "elapsed": round(elapsed, 1),
                "rate": round(rate, 2),
                "eta": round(eta, 1),
                "dpi": self.dpi,
                "color_mode": self.color_mode,
                "compression": self.compression,
                "log": list(self.log_lines[-40:]),
                "download_url": f"/api/download/{self.id}" if self.stage == "done" else "",
                "auto_delete": self.auto_delete,
                "downloads": self.downloads,
                "created_at": self.created_at,
                "expires_in": max(
                    0,
                    int(
                        (self.finished_at or self.created_at)
                        + config.JOB_TTL_MINUTES * 60
                        - time.time()
                    ),
                ),
                "active": self.stage in ("queued", "analyzing", "rendering", "packaging"),
                "version": self._version,
            }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool: ProcessPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._budget = MemoryBudget(config.RENDER_MEMORY_BYTES)
        self._drivers = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_JOBS, thread_name_prefix="job"
        )
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._pool = self._new_pool()
        log.info(
            "render pool started with %d worker(s), %d concurrent job(s), %d MB render budget",
            config.RENDER_WORKERS,
            config.MAX_CONCURRENT_JOBS,
            config.RENDER_MEMORY_BYTES // (1024 * 1024),
        )

    @staticmethod
    def _new_pool() -> ProcessPoolExecutor:
        kwargs = {"max_workers": config.RENDER_WORKERS}
        try:
            # Recycle workers periodically so long batches cannot leak memory.
            return ProcessPoolExecutor(max_tasks_per_child=250, **kwargs)
        except TypeError:  # pragma: no cover - Python < 3.11
            return ProcessPoolExecutor(**kwargs)

    def _current_pool(self) -> ProcessPoolExecutor:
        with self._pool_lock:
            assert self._pool is not None
            return self._pool

    def _replace_pool(self, broken: ProcessPoolExecutor) -> None:
        """Swap in a fresh pool once a worker died; otherwise every later job fails."""
        with self._pool_lock:
            if self._pool is not broken or self._stopping:
                return
            broken.shutdown(wait=False, cancel_futures=True)
            self._pool = self._new_pool()
        log.warning("render worker died (likely out of memory) - pool restarted")

    def shutdown(self) -> None:
        self._stopping = True
        self._drivers.shutdown(wait=False, cancel_futures=True)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    # -- job access --------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def create(
        self,
        original_name: str,
        dpi: int,
        color_mode: str,
        compression: str,
        zip_compress: bool,
        auto_delete: bool = False,
    ) -> Job:
        job_id = uuid.uuid4().hex[:16]
        job_dir = config.JOBS_DIR / job_id
        (job_dir / "tif").mkdir(parents=True, exist_ok=True)
        job = Job(
            id=job_id,
            original_name=original_name,
            dpi=dpi,
            color_mode=color_mode,
            compression=compression,
            zip_compress=zip_compress,
            auto_delete=auto_delete,
            dir=job_dir,
            pdf_path=job_dir / "source.pdf",
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def submit(self, job: Job) -> None:
        job.note(f"Queued '{job.original_name}'")
        self._drivers.submit(self._run, job)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.terminal:
            return False
        job.update(cancel_requested=True)
        job.note("Cancellation requested")
        return True

    # -- pipeline ----------------------------------------------------------
    def _run(self, job: Job) -> None:
        job.update(started_at=time.time(), stage="analyzing")
        try:
            if job.cancel_requested:
                raise JobCancelled()

            page_count, needs_pass, sizes = inspect_pdf(str(job.pdf_path))
            if needs_pass:
                raise ValueError("This PDF is password protected and cannot be opened.")
            if page_count < 1:
                raise ValueError("No pages found in this PDF.")
            if page_count > config.MAX_PAGES:
                raise ValueError(
                    f"PDF has {page_count} pages, the limit is {config.MAX_PAGES}."
                )

            job.update(total_pages=page_count, page_sizes=sizes, stage="rendering")
            job.note(f"{page_count} page(s) detected - rendering at {job.dpi} DPI")

            try:
                zip_path = self._render_and_pack(job, solo=False)
            except BrokenProcessPool:
                if job.cancel_requested:
                    raise JobCancelled() from None
                job.update(done_pages=0, bytes_written=0)
                job.note("A render worker ran out of memory - retrying one page at a time")
                try:
                    zip_path = self._render_and_pack(job, solo=True)
                except BrokenProcessPool:
                    raise RuntimeError(
                        "A page is too large to render in the memory available. "
                        "Try a lower DPI or grayscale output."
                    ) from None
            if job.cancel_requested:
                raise JobCancelled()

            job.update(
                stage="done",
                zip_name=zip_path.name,
                zip_size=zip_path.stat().st_size,
                finished_at=time.time(),
            )
            job.note(f"Ready: {zip_path.name} ({_human(zip_path.stat().st_size)})")
            self._write_meta(job)
            # The rendered pages live inside the archive now.
            shutil.rmtree(job.dir / "tif", ignore_errors=True)
            job.pdf_path.unlink(missing_ok=True)

        except JobCancelled:
            job.update(stage="cancelled", finished_at=time.time())
            job.note("Job cancelled")
            self._discard_workdir(job)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            log.exception("job %s failed", job.id)
            job.update(stage="error", error=str(exc), finished_at=time.time())
            job.note(f"Error: {exc}")
            self._discard_workdir(job)

    def _render_and_pack(self, job: Job, solo: bool) -> Path:
        """Render every page and stream it straight into the archive.

        Pages are added to the ZIP and deleted as soon as they are rendered,
        so peak disk usage stays at roughly one copy of the output instead of
        two, and there is no long silent packaging phase at the end - which on
        a 10 000 page document looked exactly like the job had hung.
        """
        out_dir = job.dir / "tif"
        stem = _safe_stem(job.original_name)
        zip_path = job.dir / f"{stem}_tiff.zip"
        pad = max(3, len(str(job.total_pages)))
        spec = RenderSpec(
            pdf_path=str(job.pdf_path),
            out_dir=str(out_dir),
            prefix=stem,
            dpi=job.dpi,
            color_mode=job.color_mode,
            compression=job.compression,
            pad=pad,
            max_pixels=config.MAX_PAGE_PIXELS,
        )

        mode = zipfile.ZIP_DEFLATED if job.zip_compress else zipfile.ZIP_STORED
        # Stored is the default: the TIFFs are already compressed, so skipping
        # deflate makes packaging effectively free.
        step = max(1, min(50, job.total_pages // 20 or 1))

        with zipfile.ZipFile(zip_path, "w", compression=mode, allowZip64=True) as archive:
            for filename, written in self._render_stream(job, spec, solo):
                page_file = out_dir / filename
                archive.write(page_file, arcname=filename)
                page_file.unlink(missing_ok=True)

                done, total_bytes = job.bump_page(written)
                if done == 1 or done % step == 0 or done == job.total_pages:
                    job.note(
                        f"Converted {done}/{job.total_pages} pages "
                        f"({_human(total_bytes)})"
                    )
                if done % config.DISK_CHECK_EVERY == 0:
                    self._check_disk(job)

        job.update(stage="packaging")
        shutil.rmtree(out_dir, ignore_errors=True)
        return zip_path

    def _render_stream(self, job: Job, spec: RenderSpec, solo: bool):
        """Yield (filename, bytes) per page, keeping only a few pages in flight.

        Submitting all 10 000 tasks up front would pickle and queue the lot,
        and cancelling would have to walk every one of them. A sliding window
        keeps memory flat and stops the moment a cancel arrives. Pages are
        also held back until their estimated memory fits the shared budget.
        """
        pool = self._current_pool()
        window = 1 if solo else max(2, config.RENDER_WORKERS * config.INFLIGHT_PER_WORKER)
        next_page = 0
        pending: set[Future] = set()

        def fill() -> None:
            nonlocal next_page
            while len(pending) < window and next_page < job.total_pages:
                width, height = job.page_sizes[next_page]
                cost = estimate_render_bytes(
                    width, height, spec.dpi, spec.color_mode, spec.max_pixels
                )
                granted = self._budget.try_acquire(cost)
                if not granted:
                    return
                try:
                    future = pool.submit(render_page, spec, next_page)
                except BaseException:
                    self._budget.release(granted)
                    raise
                future.add_done_callback(lambda _f, g=granted: self._budget.release(g))
                pending.add(future)
                next_page += 1

        try:
            fill()
            while pending or next_page < job.total_pages:
                if job.cancel_requested:
                    raise JobCancelled()
                if not pending:
                    # Another job holds the budget; wait for it to free up.
                    time.sleep(0.25)
                    fill()
                    continue
                finished, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    pending.discard(future)
                    _index, filename, written = future.result()
                    yield filename, written
                fill()
        except BrokenProcessPool:
            self._replace_pool(pool)
            raise
        finally:
            for future in pending:
                future.cancel()

    def _check_disk(self, job: Job) -> None:
        free_mb = shutil.disk_usage(job.dir).free // (1024 * 1024)
        if free_mb < config.MIN_FREE_DISK_MB:
            raise RuntimeError(
                f"Stopped at page {job.done_pages} of {job.total_pages}: only "
                f"{free_mb} MB of disk left. Try a lower DPI, grayscale, or "
                f"bilevel output."
            )

    def _discard_workdir(self, job: Job) -> None:
        shutil.rmtree(job.dir, ignore_errors=True)

    # -- persistence -------------------------------------------------------
    META_FIELDS = (
        "id", "original_name", "dpi", "color_mode", "compression", "zip_compress",
        "auto_delete", "stage", "total_pages", "done_pages", "bytes_written",
        "zip_name", "zip_size", "created_at", "started_at", "finished_at",
        "downloads",
    )

    def _write_meta(self, job: Job) -> None:
        try:
            data = {name: getattr(job, name) for name in self.META_FIELDS}
            (job.dir / "meta.json").write_text(json.dumps(data))
        except OSError:
            log.warning("could not persist metadata for job %s", job.id)

    def touch_download(self, job: Job) -> None:
        job.update(downloads=job.downloads + 1)
        self._write_meta(job)

    def restore(self) -> int:
        """Re-attach finished archives left on disk by a previous run."""
        if not config.JOBS_DIR.exists():
            return 0
        restored = 0
        for path in sorted(config.JOBS_DIR.iterdir()):
            meta_file = path / "meta.json"
            if not path.is_dir() or not meta_file.is_file():
                continue
            try:
                data = json.loads(meta_file.read_text())
            except (OSError, ValueError):
                continue
            zip_name = data.get("zip_name", "")
            if data.get("stage") != "done" or not (path / zip_name).is_file():
                shutil.rmtree(path, ignore_errors=True)
                continue
            job = Job(
                id=data["id"],
                original_name=data.get("original_name", "document.pdf"),
                dpi=data.get("dpi", config.DEFAULT_DPI),
                color_mode=data.get("color_mode", "color"),
                compression=data.get("compression", "lzw"),
                zip_compress=data.get("zip_compress", False),
                auto_delete=data.get("auto_delete", False),
                dir=path,
                pdf_path=path / "source.pdf",
            )
            for name in (
                "stage", "total_pages", "done_pages", "bytes_written", "zip_name",
                "zip_size", "created_at", "started_at", "finished_at", "downloads",
            ):
                if name in data:
                    setattr(job, name, data[name])
            job.log_lines.append("Restored after a server restart")
            with self._lock:
                self._jobs[job.id] = job
            restored += 1
        return restored

    # -- listing / removal -------------------------------------------------
    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [job.snapshot() for job in jobs[: config.LIBRARY_LIMIT]]

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if not job.terminal:
            job.update(cancel_requested=True)
            job.note("Removed while running")
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        log.info("job %s deleted", job_id)
        return True

    # -- housekeeping ------------------------------------------------------
    def cleanup(self) -> int:
        cutoff = time.time() - config.JOB_TTL_MINUTES * 60
        removed = 0
        with self._lock:
            stale = [
                job
                for job in self._jobs.values()
                if (job.finished_at or job.created_at) < cutoff
            ]
            for job in stale:
                self._jobs.pop(job.id, None)
        for job in stale:
            shutil.rmtree(job.dir, ignore_errors=True)
            removed += 1

        # Sweep directories left behind by a previous container run too.
        with self._lock:
            known = set(self._jobs)
        leftovers = list(config.JOBS_DIR.iterdir()) if config.JOBS_DIR.exists() else []
        for path in leftovers:
            if path.is_dir() and path.name not in known:
                try:
                    if path.stat().st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
                except OSError:
                    pass
        return removed


def _safe_stem(name: str) -> str:
    stem = Path(name).stem.strip() or "document"
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in stem)
    cleaned = "_".join(cleaned.split())
    return cleaned[:60] or "document"


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


manager = JobManager()
