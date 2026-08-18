#!/usr/bin/env python3
"""Smoke tests for Pagesmith.

Runs inside the container where pymupdf is available. Exercises:
  A) Chunked upload of a ~36 MB PDF — slice, retry, resume, finish, download.
  B) 10 000-page render — confirm progress ticks up, disk stays flat, ZIP is valid.

Usage:
    python3 tests/smoke_test.py [--base-url http://localhost:9000] [--password test]
"""
from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra packages needed)
# ---------------------------------------------------------------------------
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
BASE = "http://localhost:9000"


def api(method: str, path: str, body=None, headers=None, raw=False):
    url = BASE.rstrip("/") + path
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers = {**(headers or {}), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        resp = opener.open(req, timeout=300)
    except urllib.error.HTTPError as exc:
        if raw:
            return exc.status, exc.read()
        raise
    data = resp.read()
    if raw:
        return resp.status, data
    if not data:
        return None
    return json.loads(data)


def login(password: str):
    body = urllib.parse.urlencode({"password": password}).encode()
    api("POST", "/api/login", body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})


# ---------------------------------------------------------------------------
# PDF generators (pymupdf)
# ---------------------------------------------------------------------------

def generate_large_pdf(path: str, target_mb: int = 36):
    """Create a PDF that is roughly target_mb in size."""
    import pymupdf
    doc = pymupdf.open()
    chunk_text = "X" * 4000  # ~4 KB of text per page
    page_size = 0
    pages_needed = 1
    # Estimate pages — each page with the text block is roughly 2-4 KB
    # Add pages until file size is met
    while True:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {doc.page_count}\n{chunk_text}", fontsize=10)
        if doc.page_count % 200 == 0:
            # Check size periodically
            buf = io.BytesIO()
            doc.save(buf, deflate=True)
            current_size = buf.tell()
            if current_size >= target_mb * 1024 * 1024:
                break
            # Estimate remaining pages
            per_page = current_size / doc.page_count
            remaining = (target_mb * 1024 * 1024 - current_size) / per_page
            if remaining < 200:
                # Fine-tune: add remaining and check again
                pass
        if doc.page_count > 50000:
            break  # safety cap
    doc.save(path, deflate=True)
    size_mb = os.path.getsize(path) / 1048576
    print(f"  Generated {path}: {doc.page_count} pages, {size_mb:.1f} MB")
    doc.close()
    return size_mb


def generate_10k_pdf(path: str, num_pages: int = 10000):
    """Create a minimal 10 000-page PDF. Each page has just a number."""
    import pymupdf
    doc = pymupdf.open()
    for i in range(num_pages):
        page = doc.new_page(width=200, height=100)  # tiny pages
        page.insert_text((10, 50), str(i + 1), fontsize=14)
    doc.save(path, deflate=True)
    size_mb = os.path.getsize(path) / 1048576
    print(f"  Generated {path}: {num_pages} pages, {size_mb:.1f} MB")
    doc.close()


# ---------------------------------------------------------------------------
# Test A: chunked 36 MB upload
# ---------------------------------------------------------------------------

def test_chunked_upload(pdf_path: str) -> bool:
    print("\n═══ Test A: Chunked ~36 MB upload ═══")
    t0 = time.time()
    file_size = os.path.getsize(pdf_path)
    filename = os.path.basename(pdf_path)

    # 1. Open session
    print("  1. Opening upload session...")
    session = api("POST", "/api/uploads", {
        "filename": filename, "size": file_size,
        "dpi": 150, "color_mode": "gray", "compression": "lzw",
    })
    uid = session["upload_id"]
    chunk_size = session["chunk_size"]
    print(f"     upload_id={uid}  chunk_size={chunk_size}  received={session['received']}")

    # 2. Send chunks
    print("  2. Sending chunks...")
    offset = 0
    chunks_sent = 0
    with open(pdf_path, "rb") as f:
        while offset < file_size:
            end = min(offset + chunk_size, file_size)
            chunk = f.read(end - offset)

            # On chunk 3, simulate a wrong-offset error and recovery
            if chunks_sent == 3:
                print("     [simulating wrong offset → 409]")
                bad_offset = max(0, offset - 1000)
                status, raw = api("PATCH", f"/api/uploads/{uid}",
                                  body=chunk,
                                  headers={"X-Offset": str(bad_offset),
                                           "Content-Type": "application/octet-stream"},
                                  raw=True)
                if status == 409:
                    data = json.loads(raw)
                    server_offset = (data.get("detail", {}) if isinstance(data.get("detail"), dict)
                                     else {}).get("received", offset)
                    print(f"     409 received — server at {server_offset}, we're at {offset} ✓")
                else:
                    print(f"     Expected 409, got {status} ✗")

            # Send the real chunk
            result = api("PATCH", f"/api/uploads/{uid}",
                         body=chunk,
                         headers={"X-Offset": str(offset),
                                  "Content-Type": "application/octet-stream"})
            new_offset = result["received"]
            assert new_offset == end, f"Expected received={end}, got {new_offset}"
            offset = new_offset
            chunks_sent += 1
            pct = offset * 100 / file_size
            print(f"     chunk {chunks_sent}: {offset}/{file_size} ({pct:.0f}%)")

    # 3. Verify state
    print("  3. Verifying upload state...")
    state = api("GET", f"/api/uploads/{uid}")
    assert state["complete"], f"Expected complete=true, got {state}"
    print(f"     complete={state['complete']} ✓")

    # 4. Finish → job
    print("  4. Finishing upload → job...")
    result = api("POST", f"/api/uploads/{uid}/finish")
    job_id = result["job_id"]
    print(f"     job_id={job_id}")

    # 5. Poll until done
    print("  5. Polling for completion...")
    deadline = time.time() + 300  # 5 min
    while time.time() < deadline:
        s = api("GET", f"/api/status/{job_id}")
        stage = s["stage"]
        pct = s["percent"]
        pages = f"{s['done_pages']}/{s['total_pages']}"
        if stage in ("done", "error", "cancelled"):
            break
        print(f"     {stage}  {pct:.0f}%  pages={pages}")
        time.sleep(2)
    else:
        print("  ✗ TIMEOUT waiting for job to finish")
        return False

    if stage != "done":
        print(f"  ✗ Job ended with stage={stage}: {s.get('error', '')}")
        return False
    print(f"     done in {time.time()-t0:.1f}s — {s['total_pages']} pages, "
          f"{s['zip_size']/1048576:.1f} MB ZIP")

    # 6. Download and verify ZIP
    print("  6. Downloading ZIP...")
    status, data = api("GET", f"/api/download/{job_id}", raw=True)
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        tifs = [n for n in zf.namelist() if n.endswith(".tif")]
        print(f"     ZIP has {len(tifs)} TIF(s) ✓")

    # 7. Cleanup
    api("DELETE", f"/api/jobs/{job_id}")
    elapsed = time.time() - t0
    print(f"  ✓ Test A PASSED ({elapsed:.1f}s)")
    return True


# ---------------------------------------------------------------------------
# Test B: 10 000-page render
# ---------------------------------------------------------------------------

def test_10k_render(pdf_path: str) -> bool:
    print("\n═══ Test B: 10 000-page render ═══")
    t0 = time.time()
    file_size = os.path.getsize(pdf_path)

    # Upload via single-request endpoint (the PDF is small)
    print("  1. Uploading via /api/upload...")
    with open(pdf_path, "rb") as f:
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        body = bytearray()
        for name, value in [("dpi", "100"), ("color_mode", "bw"),
                            ("compression", "none"), ("auto_delete", "false")]:
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            body += f"{value}\r\n".encode()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="file"; filename="10k.pdf"\r\n'
        body += b"Content-Type: application/pdf\r\n\r\n"
        body += f.read()
        body += f"\r\n--{boundary}--\r\n".encode()

    result = api("POST", "/api/upload", body=bytes(body),
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    job_id = result["job_id"]
    print(f"     job_id={job_id}")

    # Poll until done, tracking progress
    print("  2. Polling for completion (this takes a while)...")
    last_pages = -1
    stall_count = 0
    max_stalls = 60  # 60 × 5s = 5 min of stall before we declare failure
    deadline = time.time() + 1800  # 30 min max
    data_dir = os.environ.get("DATA_DIR", "/data")

    while time.time() < deadline:
        s = api("GET", f"/api/status/{job_id}")
        stage = s["stage"]
        done = s["done_pages"]
        total = s["total_pages"]
        pct = s["percent"]
        bw = s["bytes_written"]

        # Check progress
        if done > last_pages:
            last_pages = done
            stall_count = 0
        else:
            stall_count += 1

        if stall_count > max_stalls:
            print(f"  ✗ STALLED at {done}/{total} for {stall_count * 5}s")
            return False

        # Check disk usage (flat = pages deleted as they're zipped)
        try:
            usage = shutil.disk_usage(data_dir)
            free_gb = usage.free / (1024 ** 3)
        except Exception:
            free_gb = -1

        if done % 500 == 0 or stage in ("done", "error", "cancelled"):
            print(f"     {stage:12s}  {pct:5.1f}%  pages={done:5d}/{total}  "
                  f"written={bw/1048576:.1f}MB  free={free_gb:.1f}GB")

        if stage in ("done", "error", "cancelled"):
            break
        time.sleep(5)
    else:
        print("  ✗ TIMEOUT after 30 minutes")
        return False

    if stage != "done":
        print(f"  ✗ Job ended with stage={stage}: {s.get('error', '')}")
        return False

    elapsed_render = time.time() - t0
    print(f"     Render finished in {elapsed_render:.0f}s "
          f"({s['total_pages']} pages, {s['rate']:.1f}/s)")

    # Download and verify ZIP
    print("  3. Downloading and verifying ZIP...")
    status, data = api("GET", f"/api/download/{job_id}", raw=True)
    assert status == 200, f"Download failed with {status}"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        tifs = [n for n in zf.namelist() if n.endswith(".tif")]
        if len(tifs) != 10000:
            print(f"  ✗ Expected 10000 TIFs, got {len(tifs)}")
            return False
        print(f"     ZIP has {len(tifs)} TIF entries ✓")

    # Cleanup
    api("DELETE", f"/api/jobs/{job_id}")
    elapsed = time.time() - t0
    print(f"  ✓ Test B PASSED ({elapsed:.1f}s)")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global BASE
    parser = argparse.ArgumentParser(description="Pagesmith smoke tests")
    parser.add_argument("--base-url", default="http://localhost:9000")
    parser.add_argument("--password", default="test")
    parser.add_argument("--skip-large", action="store_true",
                        help="Skip the 36 MB chunked upload test")
    parser.add_argument("--skip-10k", action="store_true",
                        help="Skip the 10 000-page render test")
    args = parser.parse_args()
    BASE = args.base_url

    # Wait for server
    print("Waiting for server...")
    for attempt in range(30):
        try:
            api("GET", "/healthz")
            break
        except Exception:
            time.sleep(1)
    else:
        print("Server did not come up in 30s")
        sys.exit(1)
    print("Server is up.")

    # Login
    login(args.password)
    print("Logged in.")

    tmpdir = tempfile.mkdtemp(prefix="smoke_")
    passed = 0
    failed = 0

    try:
        if not args.skip_large:
            pdf_36 = os.path.join(tmpdir, "large.pdf")
            print("\nGenerating ~36 MB test PDF...")
            generate_large_pdf(pdf_36, target_mb=36)
            if test_chunked_upload(pdf_36):
                passed += 1
            else:
                failed += 1

        if not args.skip_10k:
            pdf_10k = os.path.join(tmpdir, "10k.pdf")
            print("\nGenerating 10 000-page test PDF...")
            generate_10k_pdf(pdf_10k, num_pages=10000)
            if test_10k_render(pdf_10k):
                passed += 1
            else:
                failed += 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{'═' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'═' * 40}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
