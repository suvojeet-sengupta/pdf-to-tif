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
  resultTitle: $('resultTitle'), resultSummary: $('resultSummary'),
  download: $('download'), autoNote: $('autoNote'), again: $('again'),
  dropServer: $('dropServer'), failMessage: $('failMessage'), retry: $('retry'),

  panelLibrary: $('panelLibrary'), library: $('library'), refreshJobs: $('refreshJobs'),
};

const MOSAIC_LIMIT = 400;      // beyond this the tile grid stops being readable
const LIBRARY_POLL_MS = 4000;

const state = {
  file: null,
  jobId: null,
  stream: null,
  poller: null,
  libraryTimer: null,
  mosaicFor: 0,
  maxUploadBytes: 500 * 1024 * 1024,
  attached: false,
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
el.start.addEventListener('click', () => {
  if (!state.file) return;

  const body = new FormData();
  body.append('file', state.file);
  body.append('dpi', el.dpi.value);
  body.append('color_mode', el.color.value);
  body.append('compression', el.compression.value);
  body.append('zip_compress', el.zipCompress.checked ? 'true' : 'false');
  body.append('auto_delete', el.autoDelete.checked ? 'true' : 'false');

  liveView('run');
  resetRunView(`${state.file.name} · ${humanBytes(state.file.size)}`);
  el.start.disabled = true;

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.addEventListener('progress', (e) => {
    if (e.lengthComputable) paintUpload(e.loaded / e.total);
  });
  xhr.addEventListener('load', () => {
    el.start.disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* handled below */ }
    if (xhr.status === 401) return showGate();
    if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
      state.jobId = data.job_id;
      watch(data.job_id);
      refreshLibrary();
    } else {
      showFailure(data.detail || `Upload failed (HTTP ${xhr.status}).`);
    }
  });
  xhr.addEventListener('error', () => {
    el.start.disabled = false;
    showFailure('Upload failed — the connection dropped.');
  });
  xhr.send(body);
});

function resetRunView(label) {
  el.fileLabel.textContent = label;
  el.log.textContent = '';
  el.mosaic.hidden = true;
  el.mosaic.replaceChildren();
  state.mosaicFor = 0;
  el.cancel.disabled = false;
  el.cancel.textContent = 'Cancel conversion';
  paintUpload(0);
}

function paintUpload(fraction) {
  const pct = Math.round(fraction * 100);
  el.stageLabel.textContent = 'Uploading PDF';
  el.percent.innerHTML = `${pct}<i>%</i>`;
  el.barFill.style.width = `${pct}%`;
  el.statPages.textContent = '—/—';
  el.statRate.textContent = '—';
  el.statEta.textContent = '—';
  el.statElapsed.textContent = '0s';
  el.statSize.textContent = humanBytes(state.file ? state.file.size * fraction : 0);
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
  if (state.jobId) {
    try { await api(`/api/cancel/${state.jobId}`, { method: 'POST' }); } catch (_) { /* ignore */ }
  }
});

function backToIdle() {
  stopWatching();
  state.jobId = null;
  clearFile();
  liveView('idle');
  refreshLibrary();
}

el.again.addEventListener('click', backToIdle);
el.retry.addEventListener('click', backToIdle);

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
