"""Resumable chunked uploads.

A browser sends the file a few megabytes at a time and the server appends
each chunk to one file. If a chunk fails - a flaky link, a tunnel hiccup,
a phone changing network - only that chunk is retried, and a reload can
resume from the byte offset the server already holds instead of starting
the whole transfer again.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("pdf2tif.uploads")


class UploadError(Exception):
    """Raised with a message that is safe to show the user."""


@dataclass
class Upload:
    id: str
    filename: str
    size: int
    options: dict
    dir: Path
    received: int = 0
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def path(self) -> Path:
        return self.dir / "part"

    def state(self) -> dict:
        return {
            "upload_id": self.id,
            "filename": self.filename,
            "size": self.size,
            "received": self.received,
            "chunk_size": config.UPLOAD_CHUNK_BYTES,
            "complete": self.received >= self.size,
        }


class UploadStore:
    def __init__(self) -> None:
        self._uploads: dict[str, Upload] = {}
        self._lock = threading.Lock()

    def begin(self, filename: str, size: int, options: dict) -> Upload:
        if size <= 0:
            raise UploadError("The file is empty.")
        if size > config.MAX_UPLOAD_BYTES:
            raise UploadError(
                f"File is larger than the {config.MAX_UPLOAD_MB} MB limit."
            )

        upload_id = uuid.uuid4().hex[:16]
        directory = config.UPLOADS_DIR / upload_id
        directory.mkdir(parents=True, exist_ok=True)
        upload = Upload(
            id=upload_id, filename=filename, size=size, options=options, dir=directory
        )
        upload.path.touch()
        with self._lock:
            self._uploads[upload_id] = upload
        log.info("upload %s started: %s (%.1f MB)", upload_id, filename, size / 1048576)
        return upload

    def get(self, upload_id: str) -> Upload:
        with self._lock:
            upload = self._uploads.get(upload_id)
        if upload is None:
            raise UploadError("This upload has expired. Start it again.")
        return upload

    def append(self, upload_id: str, offset: int, chunk: bytes) -> Upload:
        upload = self.get(upload_id)
        with upload.lock:
            if offset != upload.received:
                # The client and server disagree on progress; tell the client
                # where we actually are so it can resend from there.
                raise UploadError(
                    f"Chunk out of order: server has {upload.received} bytes, "
                    f"chunk starts at {offset}."
                )
            if upload.received + len(chunk) > upload.size:
                raise UploadError("Chunk overruns the declared file size.")

            with open(upload.path, "ab") as handle:
                handle.write(chunk)
            upload.received += len(chunk)
            upload.touched_at = time.time()
        return upload

    def finish(self, upload_id: str) -> Upload:
        upload = self.get(upload_id)
        actual = upload.path.stat().st_size if upload.path.exists() else 0
        if actual != upload.size:
            raise UploadError(
                f"Upload is incomplete: {actual} of {upload.size} bytes arrived."
            )
        return upload

    def discard(self, upload_id: str) -> None:
        with self._lock:
            upload = self._uploads.pop(upload_id, None)
        if upload is not None:
            shutil.rmtree(upload.dir, ignore_errors=True)

    def cleanup(self) -> int:
        """Drop uploads that were abandoned part way through."""
        cutoff = time.time() - config.UPLOAD_TTL_MINUTES * 60
        with self._lock:
            stale = [u for u in self._uploads.values() if u.touched_at < cutoff]
            for upload in stale:
                self._uploads.pop(upload.id, None)
            known = set(self._uploads)

        removed = 0
        for upload in stale:
            shutil.rmtree(upload.dir, ignore_errors=True)
            removed += 1

        # Directories from a previous run of the container.
        if config.UPLOADS_DIR.exists():
            for path in list(config.UPLOADS_DIR.iterdir()):
                if path.is_dir() and path.name not in known:
                    try:
                        if path.stat().st_mtime < cutoff:
                            shutil.rmtree(path, ignore_errors=True)
                            removed += 1
                    except OSError:
                        pass
        return removed


store = UploadStore()
