"""Job store and the render -> zip pipeline."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .converter import (
    RenderSpec,
    effective_zoom,
    estimate_render_bytes,
    inspect_pdf,
    render_page,
)

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


# Unclean restarts a job survives before it is treated as the cause of them.
MAX_RESUMES = 5
# Failing this many pages in a row means something systemic, not one bad sheet.
MAX_FAILURES_IN_A_ROW = 25


class JobCancelled(Exception):
    pass


class JobInterrupted(Exception):
    """The server is shutting down; the job resumes on the next start."""


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
    # Folder jobs: (display name, path) per source PDF. Empty = just pdf_path.
    sources: list[tuple[str, Path]] = field(default_factory=list, repr=False)
    page_counts: list[int] = field(default_factory=list, repr=False)
    failed_pages: list[str] = field(default_factory=list)
    scaled_pages: int = 0
    scaled_dpi: int = 0
    token: str = ""
    resumes: int = 0

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
                "failed_count": len(self.failed_pages),
                "failed_pages": list(self.failed_pages[:50]),
                "scaled_pages": self.scaled_pages,
                "scaled_dpi": self.scaled_dpi,
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
        self._renderer = render_page
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

    def _kill_workers(self) -> None:
        # The executor has no public way to stop a hung task; killing the
        # workers breaks the pool, which the render loop already recovers from.
        with self._pool_lock:
            processes = list((getattr(self._pool, "_processes", None) or {}).values())
        for process in processes:
            process.kill()

    # -- job access --------------------------------------------------------
    def find_by_token(self, token: str) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.token == token), None)

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
        token: str = "",
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
            token=token,
            dir=job_dir,
            pdf_path=job_dir / "source.pdf",
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def submit(self, job: Job) -> None:
        job.note(f"Queued '{job.original_name}'")
        self._write_meta(job)
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

            if job.sources:
                page_count, sizes = self._inspect_folder(job)
            else:
                page_count, needs_pass, sizes = inspect_pdf(str(job.pdf_path))
                if needs_pass:
                    raise ValueError("This PDF is password protected and cannot be opened.")
                if page_count < 1:
                    raise ValueError("No pages found in this PDF.")
                job.sources = [(job.original_name, job.pdf_path)]
                job.page_counts = [page_count]
            if page_count > config.MAX_PAGES:
                raise ValueError(
                    f"PDF has {page_count} pages, the limit is {config.MAX_PAGES}."
                )

            job.update(total_pages=page_count, page_sizes=sizes, stage="rendering")
            job.note(f"{page_count} page(s) detected - rendering at {job.dpi} DPI")
            self._note_scaled_pages(job)

            zip_path = self._render_and_pack(job)
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
            shutil.rmtree(job.dir / "sources", ignore_errors=True)
            job.pdf_path.unlink(missing_ok=True)

        except JobCancelled:
            job.update(stage="cancelled", finished_at=time.time())
            job.note("Job cancelled")
            self._discard_workdir(job)
        except JobInterrupted:
            log.info("job %s left to resume after the restart", job.id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            if self._stopping:
                log.info("job %s left to resume after the restart", job.id)
                return
            log.exception("job %s failed", job.id)
            job.update(stage="error", error=str(exc), finished_at=time.time())
            job.note(f"Error: {exc}")
            self._discard_workdir(job)

    def _inspect_folder(self, job: Job) -> tuple[int, list[tuple[float, float]]]:
        """Read every source PDF; unreadable ones are skipped with a note."""
        kept: list[tuple[str, Path]] = []
        counts: list[int] = []
        sizes: list[tuple[float, float]] = []
        for name, path in job.sources:
            if job.cancel_requested:
                raise JobCancelled()
            try:
                count, needs_pass, page_sizes = inspect_pdf(str(path))
            except Exception as exc:  # noqa: BLE001
                job.failed_pages.append(f"{name}: cannot be opened ({exc})")
                job.note(f"Skipped {name}: cannot be opened ({exc})")
                continue
            if needs_pass or count < 1:
                reason = "password protected" if needs_pass else "no pages"
                job.failed_pages.append(f"{name}: {reason}")
                job.note(f"Skipped {name}: {reason}")
                continue
            kept.append((name, path))
            counts.append(count)
            sizes.extend(page_sizes)
        if not kept:
            raise ValueError("None of the PDFs in this folder could be opened.")
        job.update(sources=kept, page_counts=counts)
        job.note(f"{len(kept)} PDF(s) in folder")
        return sum(counts), sizes

    def _note_scaled_pages(self, job: Job) -> None:
        """Say so when pages exceed MAX_PAGE_PIXELS and come out below the asked DPI."""
        dpis = [
            effective_zoom(w, h, job.dpi, config.MAX_PAGE_PIXELS) * 72
            for w, h in job.page_sizes
        ]
        reduced = [d for d in dpis if round(d) < job.dpi]
        if reduced:
            lowest = round(min(reduced))
            job.update(scaled_pages=len(reduced), scaled_dpi=lowest)
            job.note(
                f"{len(reduced)} page(s) exceed the pixel limit - rendered at "
                f"{lowest} DPI instead of {job.dpi}"
            )

    def _render_and_pack(self, job: Job) -> Path:
        """Render every page and stream it straight into the archive.

        Pages are added to the ZIP and deleted as soon as they are rendered,
        so peak disk usage stays at roughly one copy of the output instead of
        two, and there is no long silent packaging phase at the end - which on
        a 10 000 page document looked exactly like the job had hung.
        """
        out_dir = job.dir / "tif"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(job.original_name)
        zip_path = job.dir / f"{stem}_tiff.zip"
        folder_job = len(job.sources) > 1 or job.sources[0][1] != job.pdf_path
        work: list[tuple[RenderSpec, int, str]] = []
        used: set[str] = set()
        for (name, path), count in zip(job.sources, job.page_counts):
            folders, leaf = _split_source_name(name) if folder_job else ([], name)
            target = out_dir.joinpath(*folders)
            target.mkdir(parents=True, exist_ok=True)
            prefix = _safe_stem(leaf) if folder_job else stem
            base, n = prefix, 2
            while f"{target}/{prefix}".lower() in used:
                prefix, n = f"{base}_{n}", n + 1
            used.add(f"{target}/{prefix}".lower())
            spec = RenderSpec(
                pdf_path=str(path),
                out_dir=str(target),
                prefix=prefix,
                dpi=job.dpi,
                color_mode=job.color_mode,
                compression=job.compression,
                pad=max(3, len(str(count))),
                max_pixels=config.MAX_PAGE_PIXELS,
                single=folder_job and count == 1,
            )
            work.extend((spec, index, name) for index in range(count))

        mode = zipfile.ZIP_DEFLATED if job.zip_compress else zipfile.ZIP_STORED
        # Stored is the default: the TIFFs are already compressed, so skipping
        # deflate makes packaging effectively free.
        step = max(1, min(50, job.total_pages // 20 or 1))

        with zipfile.ZipFile(zip_path, "w", compression=mode, allowZip64=True) as archive:
            for page_file, written in self._render_stream(job, work):
                archive.write(page_file, arcname=page_file.relative_to(out_dir).as_posix())
                page_file.unlink(missing_ok=True)

                done, total_bytes = job.bump_page(written)
                if done == 1 or done % step == 0 or done == job.total_pages:
                    job.note(
                        f"Converted {done}/{job.total_pages} pages "
                        f"({_human(total_bytes)})"
                    )
                if done % config.DISK_CHECK_EVERY == 0:
                    self._check_disk(job)

            if job.failed_pages:
                archive.writestr("FAILED_PAGES.txt", "\n".join(job.failed_pages) + "\n")

        if job.done_pages == 0:
            raise RuntimeError(
                "No page could be converted. Try a lower DPI or grayscale output."
                + (f" First problem: {job.failed_pages[0]}" if job.failed_pages else "")
            )
        job.update(stage="packaging")
        shutil.rmtree(out_dir, ignore_errors=True)
        return zip_path

    def _render_stream(self, job: Job, work: list[tuple[RenderSpec, int, str]]):
        """Yield (TIFF path, bytes) per page, keeping only a few pages in flight.

        Pages are dispatched while their estimated memory fits the shared
        budget. A worker crash never restarts the job: finished pages stay in
        the archive, the interrupted ones are retried alone with a larger
        memory estimate, and a page that still fails is reported, not fatal.
        """
        full_window = max(2, config.RENDER_WORKERS * config.INFLIGHT_PER_WORKER)
        queue = deque(range(len(work)))
        alone: set[int] = set()
        attempts: dict[int, int] = {}
        pending: dict[Future, tuple[int, ProcessPoolExecutor]] = {}
        scale = 1.0
        last_progress = time.monotonic()
        stalled = False
        failures_in_a_row = 0

        def fill() -> None:
            while queue and len(pending) < (1 if alone else full_window):
                if self._stopping:
                    raise JobInterrupted()
                index = queue[0]
                spec, page, _name = work[index]
                width, height = job.page_sizes[index]
                cost = estimate_render_bytes(
                    width, height, spec.dpi, spec.color_mode, spec.max_pixels
                )
                granted = self._budget.try_acquire(int(cost * scale))
                if not granted:
                    return
                pool = self._current_pool()
                try:
                    future = pool.submit(self._renderer, spec, page)
                except BrokenProcessPool:
                    self._budget.release(granted)
                    self._replace_pool(pool)
                    continue
                except BaseException:
                    self._budget.release(granted)
                    raise
                future.add_done_callback(lambda _f, g=granted: self._budget.release(g))
                pending[future] = (index, pool)
                queue.popleft()

        def fail(index: int, reason: str) -> None:
            nonlocal failures_in_a_row
            _spec, page, name = work[index]
            alone.discard(index)
            job.failed_pages.append(f"{name} page {page + 1}: {reason}")
            job.note(f"Could not convert {name} page {page + 1}: {reason}")
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                raise RuntimeError(
                    f"Stopped after {failures_in_a_row} pages failed in a row. "
                    f"Last problem: {reason}"
                )

        def harvest(futures) -> tuple[list, list[int]]:
            results, crashed = [], []
            for future in futures:
                index, pool = pending.pop(future)
                try:
                    results.append((index, future.result()))
                except BrokenProcessPool:
                    self._replace_pool(pool)
                    crashed.append(index)
                except CancelledError:
                    queue.appendleft(index)
                except OSError:
                    raise  # disk full or job removed; other pages would hit it too
                except Exception as exc:  # noqa: BLE001 - one bad page
                    attempts[index] = attempts.get(index, 0) + 1
                    if attempts[index] >= 2:
                        fail(index, str(exc) or type(exc).__name__)
                    else:
                        alone.add(index)
                        queue.appendleft(index)
            return results, crashed

        try:
            fill()
            while pending or queue:
                if job.cancel_requested:
                    raise JobCancelled()
                if self._stopping:
                    raise JobInterrupted()
                if not pending:
                    time.sleep(0.25)  # another job holds the budget
                    fill()
                    continue

                finished, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                if not finished:
                    if time.monotonic() - last_progress > config.PAGE_STALL_SECONDS:
                        log.warning("job %s: no page finished in time, killing workers", job.id)
                        job.note("A page stopped responding - restarting the renderer")
                        stalled = True
                        last_progress = time.monotonic()
                        self._kill_workers()
                    continue

                in_flight = len(pending)
                results, crashed = harvest(finished)
                if crashed and pending:
                    # A dead pool fails every future; collect the rest together.
                    drained, _ = wait(list(pending), timeout=30)
                    more_results, more_crashed = harvest(drained)
                    results += more_results
                    crashed += more_crashed
                    for future in list(pending):
                        crashed.append(pending.pop(future)[0])

                if crashed and in_flight == 1:
                    index = crashed[0]
                    attempts[index] = attempts.get(index, 0) + (2 if stalled else 1)
                    if attempts[index] >= 2:
                        fail(index, "too large for the available memory, or hung")
                    else:
                        alone.add(index)
                        queue.appendleft(index)
                elif crashed:
                    scale = min(4.0, scale * 2)
                    job.note(f"A render worker died - retrying {len(crashed)} page(s) one at a time")
                    alone.update(crashed)
                    queue.extendleft(sorted(crashed, reverse=True))
                if crashed:
                    stalled = False

                for index, (_page, filename, written) in results:
                    alone.discard(index)
                    failures_in_a_row = 0
                    last_progress = time.monotonic()
                    yield Path(work[index][0].out_dir) / filename, written
                fill()
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
        "downloads", "failed_pages", "scaled_pages", "scaled_dpi", "token", "resumes",
    )

    def _write_meta(self, job: Job) -> None:
        try:
            data = {name: getattr(job, name) for name in self.META_FIELDS}
            data["sources"] = [[name, str(path)] for name, path in job.sources]
            (job.dir / "meta.json").write_text(json.dumps(data))
        except OSError:
            log.warning("could not persist metadata for job %s", job.id)

    def touch_download(self, job: Job) -> None:
        job.update(downloads=job.downloads + 1)
        self._write_meta(job)

    def restore(self) -> int:
        """Re-attach finished archives and re-queue jobs a restart interrupted."""
        if not config.JOBS_DIR.exists():
            return 0
        restored = 0
        to_resume: list[Job] = []
        for path in sorted(config.JOBS_DIR.iterdir()):
            meta_file = path / "meta.json"
            if not path.is_dir() or not meta_file.is_file():
                continue
            try:
                data = json.loads(meta_file.read_text())
            except (OSError, ValueError):
                continue
            stage = data.get("stage")
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
            job.created_at = data.get("created_at", job.created_at)
            job.token = data.get("token", "")
            job.resumes = data.get("resumes", 0)

            if stage == "done" and (path / data.get("zip_name", "")).is_file():
                for name in (
                    "stage", "total_pages", "done_pages", "bytes_written", "zip_name",
                    "zip_size", "started_at", "finished_at", "downloads",
                    "failed_pages", "scaled_pages", "scaled_dpi",
                ):
                    if name in data:
                        setattr(job, name, data[name])
                job.log_lines.append("Restored after a server restart")
            elif stage == "queued":
                job.sources = [(n, Path(p)) for n, p in data.get("sources", [])]
                ready = all(p.is_file() for _n, p in job.sources) if job.sources else job.pdf_path.is_file()
                if not ready:
                    shutil.rmtree(path, ignore_errors=True)
                    continue
                if job.resumes >= MAX_RESUMES:
                    shutil.rmtree(path, ignore_errors=True)
                    job.stage = "error"
                    job.error = "This job kept stopping the server, so it was abandoned."
                    job.finished_at = time.time()
                else:
                    job.resumes += 1
                    to_resume.append(job)
            else:
                shutil.rmtree(path, ignore_errors=True)
                continue

            with self._lock:
                self._jobs[job.id] = job
            restored += 1

        for job in sorted(to_resume, key=lambda j: j.created_at):
            job.log_lines.append("Resumed from the start after a server restart")
            self.submit(job)
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
                if job.terminal and (job.finished_at or job.created_at) < cutoff
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


def _split_source_name(name: str) -> tuple[list[str], str]:
    """Split a browser-supplied relative path into safe folder names and the file.

    The path comes from the client, so "..", absolute paths and odd characters
    must never reach the filesystem or the archive.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p.strip() not in ("", ".", "..")]
    if not parts:
        return [], "document.pdf"
    return [_safe_folder(p) for p in parts[:-1]], parts[-1]


def _safe_folder(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ ()" else "_" for ch in name)
    return " ".join(cleaned.split())[:80].strip() or "folder"


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


manager = JobManager()
