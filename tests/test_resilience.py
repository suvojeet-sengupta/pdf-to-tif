#!/usr/bin/env python3
"""Failure-path tests for the render pipeline. Run where pymupdf is installed:
    python3 tests/test_resilience.py
"""
import json
import os
import signal
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ["RENDER_WORKERS"] = "3"
os.environ["MIN_FREE_DISK_MB"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from app import config, uploads  # noqa: E402
from app.converter import render_page  # noqa: E402
from app.jobs import JobManager  # noqa: E402

MARK = Path(config.DATA_DIR) / "marks"


def once(key):
    """True the first time a key is seen, across worker processes."""
    path = MARK / key
    MARK.mkdir(exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        return False


def crash_once_on_page_3(spec, page):
    if page == 3 and once("crash"):
        os.kill(os.getpid(), signal.SIGKILL)
    return render_page(spec, page)


def always_crash_on_page_3(spec, page):
    if page == 3:
        os.kill(os.getpid(), signal.SIGKILL)
    return render_page(spec, page)


def raise_once_on_page_2(spec, page):
    if page == 2 and once("raise"):
        raise ValueError("flaky")
    return render_page(spec, page)


def always_raise_on_page_2(spec, page):
    if page == 2:
        raise ValueError("bad page")
    return render_page(spec, page)


def hang_on_page_1(spec, page):
    if page == 1:
        time.sleep(600)
    return render_page(spec, page)


def make_pdf(path, pages=8):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=200, height=200).insert_text((20, 50), f"page {i}")
    doc.save(path)
    doc.close()


def run(manager, renderer, pages=8):
    manager._renderer = renderer
    job = manager.create("doc.pdf", 72, "color", "lzw", False)
    make_pdf(job.pdf_path, pages)
    manager._run(job)
    names = []
    if job.stage == "done":
        with zipfile.ZipFile(job.dir / job.zip_name) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
    return job, names


def main():
    manager = JobManager()
    manager.start()

    job, names = run(manager, crash_once_on_page_3)
    assert job.stage == "done" and job.done_pages == 8 and not job.failed_pages, job.snapshot()
    assert len(names) == 8 and len(set(names)) == 8
    print("ok: worker crash recovers, nothing lost or duplicated")

    job, names = run(manager, always_crash_on_page_3)
    assert job.stage == "done" and job.done_pages == 7 and len(job.failed_pages) == 1, job.snapshot()
    assert "FAILED_PAGES.txt" in names and "page 4" in job.failed_pages[0]
    print("ok: page that always crashes is reported, rest convert")

    job, names = run(manager, raise_once_on_page_2)
    assert job.stage == "done" and job.done_pages == 8, job.snapshot()
    print("ok: transient page error retried")

    job, names = run(manager, always_raise_on_page_2)
    assert job.done_pages == 7 and "bad page" in job.failed_pages[0], job.snapshot()
    print("ok: bad page reported")

    config.PAGE_STALL_SECONDS = 3
    job, names = run(manager, hang_on_page_1)
    config.PAGE_STALL_SECONDS = 900
    assert job.stage == "done" and job.done_pages == 7 and len(job.failed_pages) == 1, job.snapshot()
    print("ok: hung page killed and reported")

    manager._renderer = render_page
    job = manager.create("Sheets", 72, "color", "lzw", False)
    source_dir = job.dir / "sources"
    source_dir.mkdir()
    sources = []
    for i, (name, pages) in enumerate([
        ("top.pdf", 1),
        ("Area A/sheet.pdf", 1),
        ("Area B/sheet.pdf", 2),
        ("Area B/Deep/sheet.pdf", 1),
        ("Area A/sheet.PDF", 1),
        ("../../../evil/../pwn.pdf", 1),
        ("/etc/abs.pdf", 1),
        ("..\\..\\win.pdf", 1),
    ]):
        path = source_dir / f"{i:05d}.pdf"
        make_pdf(path, pages)
        sources.append((name, path))
    job.sources = sources
    manager._run(job)
    assert job.stage == "done", job.snapshot()
    with zipfile.ZipFile(job.dir / job.zip_name) as archive:
        names = sorted(archive.namelist())
    assert names == sorted([
        "top.tif",
        "Area A/sheet.tif",
        "Area A/sheet_2.tif",
        "Area B/sheet_page_001.tif",
        "Area B/sheet_page_002.tif",
        "Area B/Deep/sheet.tif",
        "evil/pwn.tif",
        "etc/abs.tif",
        "win.tif",
    ]), names
    assert not any(".." in n or n.startswith("/") for n in names)
    print("ok: subfolders kept in the ZIP, unsafe paths neutralised")

    running = manager.create("long.pdf", 72, "color", "lzw", False)
    running.created_at -= 10 * 86400
    running.update(stage="rendering")
    manager.cleanup()
    assert manager.get(running.id) is running and running.dir.exists()
    print("ok: cleanup leaves a running job alone")
    manager.delete(running.id)

    job = manager.create("queued.pdf", 72, "color", "lzw", False, token="tok")
    make_pdf(job.pdf_path, 3)
    manager._write_meta(job)
    assert manager.find_by_token("tok") is job
    fresh = JobManager()
    fresh.start()
    assert fresh.restore() >= 1
    deadline = time.time() + 30
    while fresh.get(job.id).stage != "done" and time.time() < deadline:
        time.sleep(0.2)
    assert fresh.get(job.id).done_pages == 3, fresh.get(job.id).snapshot()
    print("ok: queued job resumes after restart")
    fresh.shutdown()

    store = uploads.UploadStore()
    up = store.begin("a.pdf", 10, {"dpi": 72})
    store.append(up.id, 0, b"12345")
    with open(up.path, "ab") as handle:
        handle.write(b"junk")
    store.append(up.id, 5, b"67890")
    assert up.path.read_bytes() == b"1234567890"
    again = uploads.UploadStore()
    assert again.restore() >= 1
    assert again.get(up.id).received == 10 and again.get(up.id).options == {"dpi": 72}
    print("ok: uploads survive stray bytes and a restart")

    manager.shutdown()
    print("ALL PASSED")
    os._exit(0)


if __name__ == "__main__":
    main()
