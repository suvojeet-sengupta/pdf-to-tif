'use strict';

const $ = (id) => document.getElementById(id);

const el = {
  pick: $('pick'), drop: $('drop'), file: $('file'), chosen: $('chosen'),
  chosenName: $('chosenName'), chosenSize: $('chosenSize'), clearFile: $('clearFile'),
  dpi: $('dpi'), color: $('color'), compression: $('compression'), zipCompress: $('zipCompress'),
  start: $('start'), pickError: $('pickError'), maxSize: $('maxSize'), ttl: $('ttl'),
  progress: $('progress'), stageLabel: $('stageLabel'), fileLabel: $('fileLabel'),
  percent: $('percent'), barFill: $('barFill'), spinner: $('spinner'),
  statPages: $('statPages'), statRate: $('statRate'), statElapsed: $('statElapsed'),
  statEta: $('statEta'), statSize: $('statSize'), log: $('log'), cancel: $('cancel'),
  result: $('result'), resultTitle: $('resultTitle'), resultSummary: $('resultSummary'),
  download: $('download'), again: $('again'),
  failure: $('failure'), failMessage: $('failMessage'), retry: $('retry'),
};

let selectedFile = null;
let jobId = null;
let stream = null;
let poller = null;
let maxUploadBytes = 500 * 1024 * 1024;

/* ── helpers ──────────────────────────────────────────────────────── */
const humanBytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
};

const humanTime = (s) => {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
};

const show = (node, visible) => { node.hidden = !visible; };

function screen(name) {
  show(el.pick, name === 'pick');
  show(el.progress, name === 'progress');
  show(el.result, name === 'result');
  show(el.failure, name === 'failure');
}

/* ── file selection ───────────────────────────────────────────────── */
function setFile(file) {
  el.pickError.hidden = true;
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    return fail('Only PDF files are supported.');
  }
  if (file.size > maxUploadBytes) {
    return fail(`That file is ${humanBytes(file.size)} — the limit is ${humanBytes(maxUploadBytes)}.`);
  }
  selectedFile = file;
  el.chosenName.textContent = file.name;
  el.chosenSize.textContent = humanBytes(file.size);
  show(el.chosen, true);
  show(el.drop, false);
  el.start.disabled = false;
}

function fail(message) {
  el.pickError.textContent = message;
  el.pickError.hidden = false;
}

function clearFile() {
  selectedFile = null;
  el.file.value = '';
  show(el.chosen, false);
  show(el.drop, true);
  el.start.disabled = true;
}

el.file.addEventListener('change', () => setFile(el.file.files[0]));
el.clearFile.addEventListener('click', clearFile);

['dragenter', 'dragover'].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.remove('over'); }));
el.drop.addEventListener('drop', (e) => setFile(e.dataTransfer.files[0]));

/* Dropping anywhere on the page works too, once the picker is showing. */
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  if (!el.pick.hidden && e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});

/* ── upload ───────────────────────────────────────────────────────── */
el.start.addEventListener('click', () => {
  if (!selectedFile) return;

  const body = new FormData();
  body.append('file', selectedFile);
  body.append('dpi', el.dpi.value);
  body.append('color_mode', el.color.value);
  body.append('compression', el.compression.value);
  body.append('zip_compress', el.zipCompress.checked ? 'true' : 'false');

  screen('progress');
  el.fileLabel.textContent = `${selectedFile.name} · ${humanBytes(selectedFile.size)}`;
  el.log.textContent = '';
  el.cancel.disabled = false;
  el.cancel.textContent = 'Cancel';
  paintUpload(0);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.addEventListener('progress', (e) => {
    if (e.lengthComputable) paintUpload(e.loaded / e.total);
  });
  xhr.addEventListener('load', () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* handled below */ }
    if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
      jobId = data.job_id;
      watch(jobId);
    } else {
      showFailure(data.detail || `Upload failed (HTTP ${xhr.status}).`);
    }
  });
  xhr.addEventListener('error', () => showFailure('Upload failed — the connection dropped.'));
  xhr.addEventListener('abort', () => showFailure('Upload cancelled.'));
  xhr.send(body);
});

function paintUpload(fraction) {
  const pct = Math.round(fraction * 100);
  el.stageLabel.textContent = 'Uploading PDF';
  el.percent.textContent = `${pct}%`;
  el.barFill.style.width = `${pct}%`;
  el.statPages.textContent = '— / —';
  el.statRate.textContent = '—';
  el.statEta.textContent = '—';
  el.statSize.textContent = humanBytes(selectedFile ? selectedFile.size * fraction : 0);
}

/* ── live progress ────────────────────────────────────────────────── */
function watch(id) {
  closeStream();
  stream = new EventSource(`/api/events/${id}`);
  stream.onmessage = (event) => paint(JSON.parse(event.data));
  stream.onerror = () => {
    // Some proxies drop long-lived streams; fall back to plain polling.
    closeStream();
    if (!poller) poller = setInterval(() => pollOnce(id), 1000);
  };
}

async function pollOnce(id) {
  try {
    const res = await fetch(`/api/status/${id}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    paint(await res.json());
  } catch (err) {
    stopPolling();
    showFailure('Lost contact with the server.');
  }
}

function closeStream() { if (stream) { stream.close(); stream = null; } }
function stopPolling() { if (poller) { clearInterval(poller); poller = null; } }

function paint(state) {
  el.stageLabel.textContent = state.stage_label;
  el.percent.textContent = `${Math.round(state.percent)}%`;
  el.barFill.style.width = `${state.percent}%`;
  el.statPages.textContent = `${state.done_pages} / ${state.total_pages || '—'}`;
  el.statRate.textContent = state.rate ? `${state.rate.toFixed(1)} pg/s` : '—';
  el.statElapsed.textContent = humanTime(state.elapsed);
  el.statEta.textContent = state.eta ? humanTime(state.eta) : '—';
  el.statSize.textContent = humanBytes(state.bytes_written);

  if (state.log && state.log.length) {
    const atBottom = el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 8;
    el.log.textContent = state.log.join('\n');
    if (atBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  if (state.stage === 'done') {
    closeStream(); stopPolling();
    el.download.href = state.download_url;
    el.download.setAttribute('download', state.zip_name);
    el.resultTitle.textContent = `${state.total_pages} TIFF file${state.total_pages === 1 ? '' : 's'} ready`;
    el.resultSummary.textContent =
      `${state.zip_name} · ${humanBytes(state.zip_size)} · ${state.dpi} DPI · finished in ${humanTime(state.elapsed)}`;
    screen('result');
  } else if (state.stage === 'error') {
    closeStream(); stopPolling();
    showFailure(state.error || 'Conversion failed.');
  } else if (state.stage === 'cancelled') {
    closeStream(); stopPolling();
    showFailure('Conversion cancelled.');
  }
}

/* ── cancel / reset ───────────────────────────────────────────────── */
el.cancel.addEventListener('click', async () => {
  el.cancel.disabled = true;
  el.cancel.textContent = 'Cancelling…';
  if (jobId) {
    try { await fetch(`/api/cancel/${jobId}`, { method: 'POST' }); } catch (_) { /* ignore */ }
  }
});

function showFailure(message) {
  el.failMessage.textContent = message;
  screen('failure');
}

function reset() {
  closeStream(); stopPolling();
  jobId = null;
  clearFile();
  screen('pick');
}

el.again.addEventListener('click', reset);
el.retry.addEventListener('click', reset);

/* ── boot ─────────────────────────────────────────────────────────── */
(async function boot() {
  try {
    const cfg = await (await fetch('/api/config')).json();
    maxUploadBytes = cfg.max_upload_mb * 1024 * 1024;
    el.maxSize.textContent = cfg.max_upload_mb;
    el.ttl.textContent = cfg.job_ttl_minutes;
    [...el.dpi.options].forEach((o) => { o.selected = Number(o.value) === cfg.default_dpi; });
  } catch (_) {
    el.maxSize.textContent = '500';
    el.ttl.textContent = '120';
  }
})();
