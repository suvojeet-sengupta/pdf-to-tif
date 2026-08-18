'use strict';

/* ═══ Pagesmith front end ══════════════════════════════════════════════
   Talks to the API, keeps the live view in sync with whatever the server
   is doing, and survives a page reload mid-conversion.
   ══════════════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const el = {
  gate: $('gate'), gateForm: $('gateForm'), password: $('password'),
  gateError: $('gateError'), gateSubmit: $('gateSubmit'),
  workspace: $('workspace'), logout: $('logout'), tagWorkers: $('tagWorkers'),
  maxSize: $('maxSize'), ttl: $('ttl'),

  drop: $('drop'), file: $('file'), chosen: $('chosen'), chosenName: $('chosenName'),
  chosenSize: $('chosenSize'), clearFile: $('clearFile'), pickError: $('pickError'),
  dpi: $('dpi'), color: $('color'), compression: $('compression'),
  zipCompress: $('zipCompress'), autoDelete: $('autoDelete'), start: $('start'),

  liveDot: $('liveDot'), liveIdle: $('liveIdle'), liveRun: $('liveRun'),
  liveDone: $('liveDone'), liveFail: $('liveFail'),
  percent: $('percent'), stageLabel: $('stageLabel'), fileLabel: $('fileLabel'),
  barFill: $('barFill'), mosaic: $('mosaic'), log: $('log'), cancel: $('cancel'),
  statPages: $('statPages'), statRate: $('statRate'), statElapsed: $('statElapsed'),
  statEta: $('statEta'), statSize: $('statSize'),
  dtPages: $('dtPages'), dtRate: $('dtRate'), dtElapsed: $('dtElapsed'),
  dtEta: $('dtEta'), dtSize: $('dtSize'), retryUpload: $('retryUpload'),
  resultTitle: $('resultTitle'), resultSummary: $('resultSummary'),
  download: $('download'), autoNote: $('autoNote'), again: $('again'),
  dropServer: $('dropServer'), failMessage: $('failMessage'), retry: $('retry'),

  panelLibrary: $('panelLibrary'), library: $('library'), refreshJobs: $('refreshJobs'),
};

const MOSAIC_LIMIT = 400;      // beyond this the tile grid stops being readable
const LIBRARY_POLL_MS = 4000;
const STALL_AFTER_MS = 45000;  // no upload progress for this long = dead link

const state = {
  file: null,
  jobId: null,
  stream: null,
  poller: null,
  libraryTimer: null,
  mosaicFor: 0,
  maxUploadBytes: 500 * 1024 * 1024,
  attached: false,
  // Upload bookkeeping. `uploading` also stops the library poll from
  // resetting the view out from under an upload in flight.
  uploading: false,
  xhr: null,
  uploadStart: 0,
  lastProgressAt: 0,
  stallTimer: null,
  uploadId: null,
  cancelled: false,
  baseBytes: 0,
};

/* ── formatting ───────────────────────────────────────────────────────── */
function humanBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function humanTime(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/* ── tiny request helper; a 401 always drops you back to the gate ─────── */
async function api(path, options = {}) {
  const res = await fetch(path, { cache: 'no-store', ...options });
  if (res.status === 401) {
    showGate();
    throw new Error('unauthenticated');
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ── screens ──────────────────────────────────────────────────────────── */
function showGate() {
  stopWatching();
  clearInterval(state.libraryTimer);
  state.libraryTimer = null;
  el.workspace.hidden = true;
  el.gate.hidden = false;
  el.password.focus();
}

function showWorkspace() {
  el.gate.hidden = true;
  el.workspace.hidden = false;
  refreshLibrary();
  clearInterval(state.libraryTimer);
  state.libraryTimer = setInterval(refreshLibrary, LIBRARY_POLL_MS);
}

function liveView(name) {
  el.liveIdle.hidden = name !== 'idle';
  el.liveRun.hidden = name !== 'run';
  el.liveDone.hidden = name !== 'done';
  el.liveFail.hidden = name !== 'fail';
  el.liveDot.dataset.state =
    { idle: 'idle', run: 'busy', done: 'done', fail: 'fail' }[name] || 'idle';
}

/* ── login ────────────────────────────────────────────────────────────── */
el.gateForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  el.gateError.hidden = true;
  el.gateSubmit.disabled = true;
  el.gateSubmit.textContent = 'Checking…';

  const body = new FormData();
  body.append('password', el.password.value);

  try {
    const res = await fetch('/api/login', { method: 'POST', body });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Sign in failed.');
    el.password.value = '';
    showWorkspace();
  } catch (err) {
    el.gateError.textContent = err.message;
    el.gateError.hidden = false;
  } finally {
    el.gateSubmit.disabled = false;
    el.gateSubmit.textContent = 'Unlock';
  }
});

el.logout.addEventListener('click', async () => {
  try { await fetch('/api/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
  showGate();
});

/* ── choosing a file ──────────────────────────────────────────────────── */
function setFile(file) {
  el.pickError.hidden = true;
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    return pickError('That is not a PDF.');
  }
  if (file.size > state.maxUploadBytes) {
    return pickError(`${humanBytes(file.size)} is over the ${humanBytes(state.maxUploadBytes)} limit.`);
  }
  state.file = file;
  el.chosenName.textContent = file.name;
  el.chosenSize.textContent = humanBytes(file.size);
  el.chosen.hidden = false;
  el.drop.hidden = true;
  el.start.disabled = false;
}

function pickError(message) {
  el.pickError.textContent = message;
  el.pickError.hidden = false;
}

function clearFile() {
  state.file = null;
  el.file.value = '';
  el.chosen.hidden = true;
  el.drop.hidden = false;
  el.start.disabled = true;
}

el.file.addEventListener('change', () => setFile(el.file.files[0]));
el.clearFile.addEventListener('click', clearFile);
el.drop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.file.click(); }
});

['dragenter', 'dragover'].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.remove('over'); }));
el.drop.addEventListener('drop', (e) => setFile(e.dataTransfer.files[0]));

document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  if (!el.workspace.hidden && !el.drop.hidden && e.dataTransfer.files.length) {
    setFile(e.dataTransfer.files[0]);
  }
});

/* ── starting a conversion ────────────────────────────────────────────── */
el.start.addEventListener('click', startUpload);
el.retryUpload.addEventListener('click', startUpload);

/* Uploads go up in chunks. A dropped chunk is retried on its own instead of
   restarting the whole file, and the server's byte count - not the browser's
   send buffer - drives the progress bar, so the number means something even
   through a proxy. */
const RESUME_KEY = 'pagesmith.upload';
const MAX_CHUNK_ATTEMPTS = 6;
const DEFAULT_CHUNK = 4 * 1024 * 1024;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function detailText(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  return fallback;
}

/* Remember an in-flight upload so picking the same file again resumes it. */
function rememberUpload(id) {
  try {
    localStorage.setItem(RESUME_KEY, JSON.stringify({
      id, name: state.file.name, size: state.file.size,
    }));
  } catch (_) { /* private mode - resume is simply unavailable */ }
}

function forgetUpload() {
  try { localStorage.removeItem(RESUME_KEY); } catch (_) { /* ignore */ }
}

function rememberedUpload() {
  try {
    const saved = JSON.parse(localStorage.getItem(RESUME_KEY) || 'null');
    if (!saved || !state.file) return null;
    if (saved.name !== state.file.name || saved.size !== state.file.size) return null;
    return saved;
  } catch (_) { return null; }
}

async function startUpload() {
  if (!state.file || state.uploading) return;
  const file = state.file;

  liveView('run');
  resetRunView(`${file.name} · ${humanBytes(file.size)}`);
  el.start.disabled = true;
  el.retryUpload.hidden = true;

  state.uploading = true;
  state.cancelled = false;
  state.uploadStart = Date.now();
  state.lastProgressAt = Date.now();
  state.baseBytes = 0;
  startStallWatch();

  try {
    let session = null;
    let offset = 0;

    // Same file as an interrupted transfer? Continue from the server's offset.
    const previous = rememberedUpload();
    if (previous) {
      try {
        session = await api(`/api/uploads/${previous.id}`);
        offset = session.received || 0;
        if (offset > 0) {
          el.stageLabel.textContent = `Resuming from ${humanBytes(offset)}`;
        }
      } catch (_) {
        session = null;                       // expired; start a fresh one
      }
    }

    if (!session) {
      session = await api('/api/uploads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          size: file.size,
          dpi: Number(el.dpi.value),
          color_mode: el.color.value,
          compression: el.compression.value,
          zip_compress: el.zipCompress.checked,
          auto_delete: el.autoDelete.checked,
        }),
      });
      offset = session.received || 0;
    }

    state.uploadId = session.upload_id;
    state.baseBytes = offset;
    rememberUpload(state.uploadId);
    const chunkSize = session.chunk_size || DEFAULT_CHUNK;
    paintUpload(offset, file.size);

    while (offset < file.size) {
      if (state.cancelled) throw Object.assign(new Error('cancelled'), { aborted: true });

      let attempt = 0;
      for (;;) {
        const end = Math.min(offset + chunkSize, file.size);
        try {
          offset = await sendChunk(state.uploadId, file.slice(offset, end), offset, file.size);
          break;
        } catch (err) {
          if (err.fatal) throw err;
          attempt += 1;
          if (attempt >= MAX_CHUNK_ATTEMPTS) throw err;
          el.stageLabel.textContent =
            `Connection problem — retrying (${attempt}/${MAX_CHUNK_ATTEMPTS - 1})`;
          await sleep(Math.min(8000, 400 * 2 ** attempt));
          if (state.cancelled) throw Object.assign(new Error('cancelled'), { aborted: true });
          // The chunk may have landed before the link died - resync and go on.
          try { offset = (await api(`/api/uploads/${state.uploadId}`)).received; }
          catch (_) { /* keep the offset we have and retry it */ }
          if (offset >= file.size) break;
        }
      }
      paintUpload(offset, file.size);
    }

    paintHandoff();
    const created = await api(`/api/uploads/${state.uploadId}/finish`, { method: 'POST' });
    forgetUpload();
    state.uploadId = null;
    state.jobId = created.job_id;
    finishUpload();
    watch(created.job_id);
    refreshLibrary();
  } catch (err) {
    finishUpload();
    if (err.auth || err.message === 'unauthenticated') return;   // gate already shown
    if (err.aborted || state.cancelled) {
      await discardUpload();
      backToIdle();
      return;
    }
    showUploadFailure(uploadMessage(err), true);
  }
}

function sendChunk(uploadId, blob, offset, total) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open('PATCH', `/api/uploads/${uploadId}`);
    xhr.setRequestHeader('X-Offset', String(offset));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');

    xhr.upload.onprogress = (event) => {
      state.lastProgressAt = Date.now();
      if (event.lengthComputable) paintUpload(offset + event.loaded, total);
    };

    xhr.onload = () => {
      state.xhr = null;
      state.lastProgressAt = Date.now();
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* below */ }

      if (xhr.status >= 200 && xhr.status < 300) return resolve(data.received);
      if (xhr.status === 401) {
        showGate();
        return reject(Object.assign(new Error('unauthenticated'), { fatal: true, auth: true }));
      }
      // 409 means the server is at a different offset; it tells us which.
      if (xhr.status === 409 && data.detail && typeof data.detail.received === 'number') {
        return resolve(data.detail.received);
      }
      if ([400, 404, 409, 413].includes(xhr.status)) {
        return reject(Object.assign(
          new Error(detailText(data, `Upload rejected (HTTP ${xhr.status}).`)),
          { fatal: true },
        ));
      }
      reject(new Error(detailText(data, `HTTP ${xhr.status}`)));
    };

    xhr.onerror = () => { state.xhr = null; reject(new Error('network')); };
    xhr.ontimeout = () => { state.xhr = null; reject(new Error('timeout')); };
    xhr.onabort = () => {
      state.xhr = null;
      // A stall abort should retry the chunk; a user cancel should not.
      if (state.cancelled) {
        reject(Object.assign(new Error('cancelled'), { aborted: true, fatal: true }));
      } else {
        reject(new Error('stalled'));
      }
    };

    xhr.send(blob);
  });
}

async function discardUpload() {
  if (!state.uploadId) return;
  const id = state.uploadId;
  state.uploadId = null;
  forgetUpload();
  try { await api(`/api/uploads/${id}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
}

function uploadMessage(err) {
  const raw = err && err.message ? err.message : '';
  if (raw === 'network' || raw === 'stalled' || raw === 'timeout') {
    return 'The connection kept failing while uploading. Your progress is held '
         + 'on the server — pick the same file again to resume where it stopped.';
  }
  if (/413|larger than/i.test(raw)) {
    return `${raw} Cloudflare's free plan also caps a single request at 100 MB, `
         + 'which the chunked upload stays under.';
  }
  return raw || 'The upload failed.';
}

function finishUpload() {
  state.uploading = false;
  state.xhr = null;
  el.start.disabled = !state.file;
  clearInterval(state.stallTimer);
  state.stallTimer = null;
}

function startStallWatch() {
  clearInterval(state.stallTimer);
  state.stallTimer = setInterval(() => {
    if (!state.uploading) return;
    if (Date.now() - state.lastProgressAt > STALL_AFTER_MS && state.xhr) {
      state.xhr.abort();        // retried by the chunk loop, not fatal
    }
  }, 5000);
}

function showUploadFailure(message, offerRetry) {
  el.failMessage.textContent = message;
  el.retryUpload.hidden = !offerRetry || !state.file;
  el.retryUpload.textContent = state.uploadId ? 'Resume upload' : 'Try again';
  liveView('fail');
}

function resetRunView(label) {
  el.fileLabel.textContent = label;
  el.log.textContent = '';
  el.mosaic.hidden = true;
  el.mosaic.replaceChildren();
  state.mosaicFor = 0;
  el.cancel.disabled = false;
  el.cancel.textContent = 'Cancel';
  paintUpload(0, state.file ? state.file.size : 0);
}

function statLabels(a, b, c, d, e) {
  el.dtPages.textContent = a;
  el.dtRate.textContent = b;
  el.dtElapsed.textContent = c;
  el.dtEta.textContent = d;
  el.dtSize.textContent = e;
}

function paintUpload(loaded, total) {
  const pct = total ? (loaded / total) * 100 : 0;
  const elapsed = (Date.now() - state.uploadStart) / 1000;
  const moved = Math.max(0, loaded - (state.baseBytes || 0));
  const rate = elapsed > 0.4 ? moved / elapsed : 0;       // bytes per second
  const left = rate > 0 ? (total - loaded) / rate : 0;

  el.stageLabel.textContent = 'Uploading to the server';
  el.percent.innerHTML = `${Math.round(pct)}<i>%</i>`;
  el.barFill.style.width = `${pct}%`;

  statLabels('Sent', 'Speed', 'Elapsed', 'Left', 'Total');
  el.statPages.textContent = humanBytes(loaded);
  el.statRate.textContent = rate ? `${humanBytes(rate)}/s` : '—';
  el.statElapsed.textContent = humanTime(elapsed);
  el.statEta.textContent = rate ? humanTime(left) : '—';
  el.statSize.textContent = humanBytes(total);
}

/* Upload bytes are all out; the server is still reading and saving them. */
function paintHandoff() {
  el.stageLabel.textContent = 'Upload complete — server is receiving';
  el.percent.innerHTML = `100<i>%</i>`;
  el.barFill.style.width = '100%';
  el.statEta.textContent = '—';
}

/* ── watching a job ───────────────────────────────────────────────────── */
function watch(id) {
  stopWatching();
  state.jobId = id;
  state.attached = true;
  state.stream = new EventSource(`/api/events/${id}`);
  state.stream.onmessage = (event) => paint(JSON.parse(event.data));
  state.stream.onerror = () => {
    // Some proxies cut long-lived streams; fall back to polling.
    if (state.stream) { state.stream.close(); state.stream = null; }
    if (!state.poller) state.poller = setInterval(() => pollOnce(id), 1200);
  };
}

async function pollOnce(id) {
  try {
    paint(await api(`/api/status/${id}`));
  } catch (err) {
    if (err.message === 'unauthenticated') return;
    stopWatching();
    showFailure('Lost contact with the server.');
  }
}

function stopWatching() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  if (state.poller) { clearInterval(state.poller); state.poller = null; }
  state.attached = false;
}

function buildMosaic(total) {
  if (total === state.mosaicFor) return;
  state.mosaicFor = total;
  if (!total || total > MOSAIC_LIMIT) { el.mosaic.hidden = true; return; }
  const tiles = document.createDocumentFragment();
  for (let i = 0; i < total; i += 1) tiles.appendChild(document.createElement('i'));
  el.mosaic.replaceChildren(tiles);
  el.mosaic.hidden = false;
}

function paintMosaic(done) {
  if (el.mosaic.hidden) return;
  const tiles = el.mosaic.children;
  for (let i = 0; i < tiles.length; i += 1) tiles[i].classList.toggle('on', i < done);
}

function paint(s) {
  if (s.total_pages) buildMosaic(s.total_pages);

  statLabels('Pages', 'Rate', 'Elapsed', 'Left', 'Written');
  el.stageLabel.textContent = s.stage_label;
  el.percent.innerHTML = `${Math.round(s.percent)}<i>%</i>`;
  el.barFill.style.width = `${s.percent}%`;
  el.fileLabel.textContent = s.original_name;
  el.statPages.textContent = `${s.done_pages}/${s.total_pages || '—'}`;
  el.statRate.textContent = s.rate ? `${s.rate.toFixed(1)}/s` : '—';
  el.statElapsed.textContent = humanTime(s.elapsed);
  el.statEta.textContent = s.eta ? humanTime(s.eta) : '—';
  el.statSize.textContent = humanBytes(s.bytes_written);
  paintMosaic(s.done_pages);

  if (s.log && s.log.length) {
    const atBottom = el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 10;
    el.log.textContent = s.log.join('\n');
    if (atBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  if (s.stage === 'done') {
    stopWatching();
    el.download.href = s.download_url;
    el.download.setAttribute('download', s.zip_name);
    el.resultTitle.textContent =
      `${s.total_pages} TIFF${s.total_pages === 1 ? '' : 's'} ready`;
    el.resultSummary.textContent =
      `${s.zip_name} · ${humanBytes(s.zip_size)} · ${s.dpi} dpi · ${humanTime(s.elapsed)}`;
    el.autoNote.hidden = !s.auto_delete;
    el.dropServer.dataset.job = s.id;
    liveView('done');
    refreshLibrary();
  } else if (s.stage === 'error') {
    stopWatching();
    showFailure(s.error || 'Conversion failed.');
  } else if (s.stage === 'cancelled') {
    stopWatching();
    showFailure('Conversion cancelled.');
  } else {
    liveView('run');
  }
}

function showFailure(message) {
  el.failMessage.textContent = message;
  liveView('fail');
  refreshLibrary();
}

/* ── cancel / reset / delete ──────────────────────────────────────────── */
el.cancel.addEventListener('click', async () => {
  el.cancel.disabled = true;
  el.cancel.textContent = 'Cancelling…';
  if (state.uploading) {
    state.cancelled = true;
    if (state.xhr) state.xhr.abort();
    return;
  }
  if (state.jobId) {
    try { await api(`/api/cancel/${state.jobId}`, { method: 'POST' }); } catch (_) { /* ignore */ }
  }
});

function backToIdle() {
  stopWatching();
  finishUpload();
  el.retryUpload.hidden = true;
  state.jobId = null;
  clearFile();
  liveView('idle');
  refreshLibrary();
}

el.again.addEventListener('click', backToIdle);
el.retry.addEventListener('click', backToIdle);

// Leaving mid-upload would silently drop the transfer; warn first.
window.addEventListener('beforeunload', (event) => {
  if (!state.uploading) return;
  event.preventDefault();
  event.returnValue = '';
});

el.dropServer.addEventListener('click', async () => {
  const id = el.dropServer.dataset.job;
  if (!id) return backToIdle();
  el.dropServer.disabled = true;
  try { await api(`/api/jobs/${id}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
  el.dropServer.disabled = false;
  backToIdle();
});

/* The archive is removed server side right after an auto-delete download. */
el.download.addEventListener('click', () => {
  if (el.autoNote.hidden) return;
  setTimeout(() => { backToIdle(); }, 2500);
});

/* ── library ──────────────────────────────────────────────────────────── */
async function refreshLibrary() {
  let jobs;
  try {
    ({ jobs } = await api('/api/jobs'));
  } catch (_) {
    return;
  }

  el.panelLibrary.hidden = jobs.length === 0;
  el.library.replaceChildren(...jobs.map(renderLibraryRow));

  // While a file is going up there is no server-side job yet. Without this
  // guard the poll below would decide nothing is running and reset the view
  // to idle mid-upload, which looks exactly like the upload vanishing.
  if (state.uploading) return;

  // Reconnect to a conversion that is still running (e.g. after a reload).
  if (!state.attached) {
    const running = jobs.find((job) => job.active);
    const showingResult = !el.liveDone.hidden || !el.liveFail.hidden;
    if (running) {
      liveView('run');
      buildMosaic(running.total_pages);
      watch(running.id);
      paint(running);
    } else if (!showingResult) {
      liveView('idle');
    }
  }
}

function renderLibraryRow(job) {
  const row = document.createElement('li');
  row.className = 'lib';

  const dot = document.createElement('span');
  dot.className = 'lib__state';
  dot.dataset.state = job.active ? 'active' : job.stage === 'done' ? 'done' : 'fail';

  const meta = document.createElement('span');
  meta.className = 'lib__meta';
  const name = document.createElement('b');
  name.textContent = job.original_name;
  const sub = document.createElement('small');
  sub.textContent = job.active
    ? `${job.stage_label} · ${job.done_pages}/${job.total_pages || '?'} pages`
    : `${job.total_pages} pages · ${humanBytes(job.zip_size)} · expires in ${humanTime(job.expires_in)}`;
  meta.append(name, sub);

  const acts = document.createElement('span');
  acts.className = 'lib__acts';

  if (job.active) {
    acts.appendChild(button('View', 'btn btn--icon', () => {
      liveView('run');
      buildMosaic(job.total_pages);
      watch(job.id);
      paint(job);
    }));
  } else if (job.stage === 'done') {
    const link = document.createElement('a');
    link.className = 'btn btn--icon';
    link.href = job.download_url;
    link.setAttribute('download', job.zip_name);
    link.textContent = 'Download';
    acts.appendChild(link);
  }

  acts.appendChild(button('Delete', 'btn btn--icon btn--danger', async (event) => {
    event.currentTarget.disabled = true;
    try { await api(`/api/jobs/${job.id}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
    if (state.jobId === job.id) backToIdle(); else refreshLibrary();
  }));

  row.append(dot, meta, acts);
  return row;
}

function button(label, className, handler) {
  const node = document.createElement('button');
  node.type = 'button';
  node.className = className;
  node.textContent = label;
  node.addEventListener('click', handler);
  return node;
}

el.refreshJobs.addEventListener('click', refreshLibrary);

/* ── boot ─────────────────────────────────────────────────────────────── */
(async function boot() {
  let cfg = {};
  try {
    cfg = await (await fetch('/api/config', { cache: 'no-store' })).json();
  } catch (_) {
    cfg = { max_upload_mb: 500, job_ttl_minutes: 120, authenticated: true, auth_enabled: false };
  }

  state.maxUploadBytes = (cfg.max_upload_mb || 500) * 1024 * 1024;
  el.maxSize.textContent = cfg.max_upload_mb ?? '—';
  el.ttl.textContent = cfg.job_ttl_minutes ?? '—';
  el.tagWorkers.textContent = `${cfg.render_workers || '?'} workers`;
  el.logout.hidden = !cfg.auth_enabled;
  if (cfg.default_dpi) {
    [...el.dpi.options].forEach((o) => { o.selected = Number(o.value) === cfg.default_dpi; });
  }
  document.title = `${cfg.app_name || 'Pagesmith'} · PDF to TIFF`;

  liveView('idle');
  if (cfg.auth_enabled && !cfg.authenticated) showGate();
  else showWorkspace();
})();
