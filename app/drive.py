"""Upload finished archives to Google Drive with rclone, then share the folder."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("pdf2tif.drive")

STAGE_LABELS = {
    "queued": "Waiting",
    "preparing": "Unpacking archives",
    "uploading": "Uploading to Google Drive",
    "sharing": "Creating share link",
    "done": "Uploaded",
    "error": "Failed",
    "cancelled": "Cancelled",
}
ACTIVE = ("queued", "preparing", "uploading", "sharing")


class DriveError(Exception):
    pass


class UploadCancelled(Exception):
    pass


@dataclass
class DriveUpload:
    id: str
    folder: str
    job_ids: list[str]
    names: list[str]
    stage: str = "queued"
    message: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    speed: float = 0.0
    eta: float = 0.0
    attempt: int = 0
    link: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel_requested: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _process: subprocess.Popen | None = field(default=None, repr=False)

    PERSISTED = (
        "id", "folder", "job_ids", "names", "stage", "message", "bytes_done", "bytes_total",
        "files_done", "files_total", "link", "error", "created_at", "finished_at",
    )

    def update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            percent = round(self.bytes_done / self.bytes_total * 100, 1) if self.bytes_total else 0.0
            if self.stage in ("sharing", "done"):
                percent = 100.0
            return {
                "id": self.id,
                "folder": self.folder,
                "names": list(self.names),
                "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "message": self.message,
                "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "files_done": self.files_done,
                "files_total": self.files_total,
                "percent": percent,
                "speed": round(self.speed),
                "eta": round(self.eta),
                "attempt": self.attempt,
                "link": self.link,
                "error": self.error,
                "created_at": self.created_at,
                "active": self.stage in ACTIVE,
            }


class DriveUploader:
    def __init__(self) -> None:
        self._uploads: dict[str, DriveUpload] = {}
        self._lock = threading.Lock()
        self._runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive")
        config.DRIVE_DIR.mkdir(parents=True, exist_ok=True)

    # -- setup ---------------------------------------------------------------
    @property
    def remote_root(self) -> str:
        base = config.GDRIVE_FOLDER.strip("/")
        return f"{config.GDRIVE_REMOTE}:{base}" if base else f"{config.GDRIVE_REMOTE}:"

    def check_setup(self) -> None:
        """Raise DriveError with a message the user can act on when rclone cannot reach Drive."""
        if shutil.which("rclone") is None:
            raise DriveError("Google Drive upload is not available: rclone is not installed on the server.")
        try:
            remotes = _rclone(["listremotes"], timeout=20).stdout.split()
        except subprocess.TimeoutExpired:
            raise DriveError("Google Drive upload is not available: rclone did not respond.") from None
        if f"{config.GDRIVE_REMOTE}:" not in remotes:
            raise DriveError(
                f"Google Drive is not set up: rclone has no '{config.GDRIVE_REMOTE}' remote. "
                "Run 'rclone config' on the server and copy the config to the app."
            )
        try:
            result = _rclone(["mkdir", self.remote_root], timeout=60)
        except subprocess.TimeoutExpired:
            raise DriveError("Google Drive did not answer in time. Try again in a minute.") from None
        if result.returncode != 0:
            raise DriveError(f"Google Drive sign-in failed: {_last_error(result.stderr)}")

    # -- access --------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            uploads = sorted(self._uploads.values(), key=lambda u: u.created_at, reverse=True)
        return [u.snapshot() for u in uploads[: config.LIBRARY_LIMIT]]

    def pinned_jobs(self) -> set[str]:
        with self._lock:
            return {jid for u in self._uploads.values() if u.stage in ("queued", "preparing") for jid in u.job_ids}

    def start(self, folder: str, jobs: list) -> DriveUpload:
        upload = DriveUpload(
            id=uuid.uuid4().hex[:12],
            folder=folder,
            job_ids=[job.id for job in jobs],
            names=[job.original_name for job in jobs],
        )
        with self._lock:
            self._uploads[upload.id] = upload
        self._save(upload)
        self._runner.submit(self._run, upload, jobs)
        return upload

    def cancel(self, upload_id: str) -> bool:
        with self._lock:
            upload = self._uploads.get(upload_id)
        if upload is None or upload.stage not in ACTIVE:
            return False
        upload.update(cancel_requested=True)
        _kill(upload._process)
        return True

    def remove(self, upload_id: str) -> bool:
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is None or upload.stage in ACTIVE:
                return False
            self._uploads.pop(upload_id)
        (config.DRIVE_DIR / f"{upload_id}.json").unlink(missing_ok=True)
        return True

    # -- pipeline ------------------------------------------------------------
    def _run(self, upload: DriveUpload, jobs: list) -> None:
        staging = config.DRIVE_DIR / upload.id
        try:
            upload.update(stage="preparing")
            self._save(upload)
            folder = self._free_folder_name(upload.folder)
            upload.update(folder=folder)
            root = staging / folder
            self._unpack(upload, jobs, root)
            if upload.cancel_requested:
                raise UploadCancelled()

            upload.update(stage="uploading", message="")
            self._save(upload)
            target = f"{self.remote_root}/{folder}"
            self._copy(upload, root, target)

            upload.update(stage="sharing", message="")
            self._save(upload)
            link = self._share(target)
            upload.update(stage="done", link=link, finished_at=time.time(), speed=0.0, eta=0.0)
            log.info("drive upload %s finished: %s", upload.id, link)
        except UploadCancelled:
            upload.update(stage="cancelled", finished_at=time.time(), message="")
        except Exception as exc:  # noqa: BLE001 - shown to the user
            log.exception("drive upload %s failed", upload.id)
            upload.update(stage="error", error=str(exc), finished_at=time.time(), message="")
        finally:
            upload.update(_process=None)
            shutil.rmtree(staging, ignore_errors=True)
            self._save(upload)

    def _free_folder_name(self, wanted: str) -> str:
        """Never merge into a folder that already exists on Drive."""
        result = _rclone(["lsf", "--dirs-only", self.remote_root], timeout=120)
        existing = {line.rstrip("/") for line in result.stdout.splitlines()} if result.returncode == 0 else set()
        name, n = wanted, 2
        while name in existing:
            name = f"{wanted} ({n})"
            n += 1
        return name

    def _unpack(self, upload: DriveUpload, jobs: list, root: Path) -> None:
        archives = []
        for job in jobs:
            zip_path = job.dir / job.zip_name
            if not zip_path.is_file():
                raise DriveError(f"The archive for '{job.original_name}' has expired.")
            archives.append((job, zip_path))

        with_zip = [(job, path, zipfile.ZipFile(path)) for job, path in archives]
        try:
            needed = sum(info.file_size for _, _, zf in with_zip for info in zf.infolist())
            free = shutil.disk_usage(config.DATA_DIR).free - config.MIN_FREE_DISK_MB * 1024 * 1024
            if needed > free:
                raise DriveError(
                    f"Not enough disk space to unpack ({_human(needed)} needed, {_human(max(free, 0))} free)."
                )
            files = sum(1 for _, _, zf in with_zip for info in zf.infolist() if not info.is_dir())
            upload.update(bytes_total=needed, files_total=files)

            single = len(with_zip) == 1
            used: set[str] = set()
            for index, (job, path, zf) in enumerate(with_zip, 1):
                if upload.cancel_requested:
                    raise UploadCancelled()
                upload.update(message=f"Unpacking {index}/{len(with_zip)}: {job.original_name}")
                if single:
                    dest = root
                else:
                    sub = _unique(_folder_name(Path(job.zip_name).stem), used)
                    dest = root / sub
                dest.mkdir(parents=True, exist_ok=True)
                zf.extractall(dest)
        finally:
            for _, _, zf in with_zip:
                zf.close()

    def _copy(self, upload: DriveUpload, source: Path, target: str) -> None:
        errors: deque[str] = deque(maxlen=5)
        for attempt in range(1, config.DRIVE_RETRIES + 1):
            upload.update(attempt=attempt, bytes_done=0, files_done=0)
            if attempt > 1:
                upload.update(message=f"Retrying (attempt {attempt}/{config.DRIVE_RETRIES})")
            code, stalled = self._copy_once(upload, source, target, errors)
            if upload.cancel_requested:
                raise UploadCancelled()
            if code == 0:
                return
            reason = "no progress for " + _human_time(config.DRIVE_STALL_SECONDS) if stalled else (
                errors[-1] if errors else f"rclone exited with code {code}"
            )
            log.warning("drive upload %s attempt %d failed: %s", upload.id, attempt, reason)
            if attempt < config.DRIVE_RETRIES:
                time.sleep(min(30, 5 * attempt))
        raise DriveError(f"Upload to Google Drive failed after {config.DRIVE_RETRIES} tries: {reason}")

    def _copy_once(self, upload: DriveUpload, source: Path, target: str, errors: deque) -> tuple[int, bool]:
        process = subprocess.Popen(
            [
                "rclone", "copy", str(source), target,
                "--transfers", "4", "--checkers", "8",
                "--drive-chunk-size", "64M",
                "--contimeout", "30s", "--timeout", "120s",
                "--retries", "1", "--low-level-retries", "10",
                "--stats", "2s", "--stats-log-level", "NOTICE",
                "--use-json-log", "--log-level", "NOTICE",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        upload.update(_process=process)
        progress = {"at": time.monotonic(), "mark": (0, 0, 0)}

        def read() -> None:
            for line in process.stderr:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                stats = entry.get("stats")
                if stats:
                    done, files = int(stats.get("bytes") or 0), int(stats.get("transfers") or 0)
                    upload.update(
                        bytes_done=done,
                        bytes_total=max(int(stats.get("totalBytes") or 0), done),
                        files_done=files,
                        files_total=max(int(stats.get("totalTransfers") or 0), files),
                        speed=float(stats.get("speed") or 0),
                        eta=float(stats.get("eta") or 0),
                    )
                    mark = (done, files, int(stats.get("checks") or 0))
                    if mark != progress["mark"]:
                        progress.update(at=time.monotonic(), mark=mark)
                elif entry.get("level") in ("error", "critical"):
                    errors.append(_clean(entry.get("msg", "")))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        stalled = False
        while process.poll() is None:
            if upload.cancel_requested:
                _kill(process)
            elif time.monotonic() - progress["at"] > config.DRIVE_STALL_SECONDS:
                stalled = True
                _kill(process)
            time.sleep(1)
        reader.join(timeout=5)
        return process.returncode, stalled

    def _share(self, target: str) -> str:
        last = ""
        for attempt in range(3):
            try:
                result = _rclone(["link", target], timeout=120)
            except subprocess.TimeoutExpired:
                last = "Google Drive did not answer in time"
                continue
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[-1]
            last = _last_error(result.stderr)
            time.sleep(3 * (attempt + 1))
        raise DriveError(f"Files are uploaded, but the share link could not be created: {last}")

    # -- persistence ---------------------------------------------------------
    def _save(self, upload: DriveUpload) -> None:
        try:
            data = {name: getattr(upload, name) for name in DriveUpload.PERSISTED}
            (config.DRIVE_DIR / f"{upload.id}.json").write_text(json.dumps(data))
        except OSError:
            log.warning("could not save drive upload %s", upload.id)

    def restore(self) -> int:
        restored = 0
        for path in config.DRIVE_DIR.glob("*.json"):
            try:
                upload = DriveUpload(**json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if upload.stage in ACTIVE:
                upload.stage = "error"
                upload.error = "The server restarted during this upload. Start it again."
                upload.finished_at = time.time()
                shutil.rmtree(config.DRIVE_DIR / upload.id, ignore_errors=True)
                self._save(upload)
            with self._lock:
                self._uploads[upload.id] = upload
            restored += 1
        return restored

    def shutdown(self) -> None:
        with self._lock:
            uploads = list(self._uploads.values())
        for upload in uploads:
            _kill(upload._process)
        self._runner.shutdown(wait=False, cancel_futures=True)


def _rclone(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)


def _kill(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def _clean(message: str) -> str:
    return " ".join(message.split())[:300]


def _last_error(stderr: str) -> str:
    lines = [
        line for line in stderr.splitlines()
        if line.strip() and "shared Google Drive client_id" not in line
    ]
    if not lines:
        return "unknown error"
    text = lines[-1]
    for marker in ("ERROR : ", "CRITICAL: ", "Failed to "):
        if marker in text:
            text = text[text.index(marker):].removeprefix("ERROR : ").removeprefix("CRITICAL: ")
            break
    return _clean(text)


def safe_folder(name: str) -> str:
    """A Drive folder name: no path separators, no surrounding blanks."""
    cleaned = " ".join(name.replace("/", "_").replace("\\", "_").split()).strip(" .")
    return cleaned[:120]


def _folder_name(name: str) -> str:
    return safe_folder(name) or "archive"


def _unique(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{name} ({n})"
        n += 1
    used.add(candidate)
    return candidate


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _human_time(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


uploader = DriveUploader()
