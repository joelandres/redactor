// Document Redactor front end (no framework, no build step).
// Documents live only in this browser's private, in-memory workspace on the server.
const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const res = await fetch(url, opts);
  const body = res.headers.get('content-type')?.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.detail ? JSON.stringify(body.detail).replace(/^"|"$/g, '') : res.statusText);
  return body;
};
// which = "original" or a redaction id
const fileUrl = (docId, which, path = 'file') =>
  `/api/documents/${encodeURIComponent(docId)}/files/${encodeURIComponent(which)}/${path}`;

let categoryLabels = {};   // category id -> checkbox label
let documents = [];        // from /api/workspace, upload order

const currentDoc = () => documents.find((d) => d.id === $('doc').value);
const currentVersion = () => currentDoc()?.redactions.find((r) => r.id === $('cmp-version').value);

// "Names, Addresses · Nov 18, 9:00 AM" for a redacted version.
function describeVersion(v) {
  if (!v) return '';
  const ids = v.categories || [];
  const all = Object.keys(categoryLabels);
  const cats = all.length && all.every((id) => ids.includes(id)) ? 'All categories'
    : [...all.filter((id) => ids.includes(id)), ...ids.filter((id) => !all.includes(id))]
        .map((id) => categoryLabels[id] || id).join(', ');
  const when = new Date(v.created * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  return [cats, when].filter(Boolean).join(' · ');
}

// ─────────────────────────── workspace ───────────────────────────
// Reload documents and versions; optionally select a document and/or version.
async function loadWorkspace({ doc, version } = {}) {
  const ws = await api('/api/workspace');
  documents = ws.documents;
  $('privacy-note').textContent =
    `Your files are kept only in server memory for this browser session and are never saved. ` +
    `They're removed when you click Remove, or after ${ws.idle_minutes} minutes of inactivity.`;

  const selected = doc ?? $('doc').value;
  $('doc').replaceChildren(...documents.map((d) => new Option(d.name, d.id, false, d.id === selected)));
  if (!documents.length) $('doc').append(new Option('No documents yet: upload a PDF', ''));
  showVersions(version);
}

function showVersions(select) {
  const versions = currentDoc()?.redactions ?? [];   // newest first
  $('cmp-version').replaceChildren(...versions.map((v, i) =>
    new Option(v.name, v.id, false, select ? v.id === select : i === 0)));
  $('cmp-version').disabled = !versions.length;
  updateButtons();
  return renderCompare();
}

async function init() {
  const cats = await api('/api/categories');
  categoryLabels = Object.fromEntries(cats.map((c) => [c.id, c.label]));
  const groups = new Map();
  for (const c of cats) {
    if (!groups.has(c.group)) {
      const section = Object.assign(document.createElement('div'), { className: 'cat-group' });
      section.append(Object.assign(document.createElement('div'), { className: 'cat-group-title', textContent: c.group }));
      groups.set(c.group, section);
      $('cats').append(section);
    }
    const label = document.createElement('label');
    const box = Object.assign(document.createElement('input'), { type: 'checkbox', value: c.id, checked: c.default });
    label.append(box, ' ', c.label);
    groups.get(c.group).append(label);
  }
  await loadWorkspace();
}

const selectedCategories = () => [...document.querySelectorAll('#cats input:checked')].map((i) => i.value);

// ─────────────────────────── messages & busy state ───────────────────────────
function show(text, cls = '') {
  const out = $('output');
  out.hidden = false;
  out.className = `output ${cls}`;
  out.textContent = text;   // textContent, never innerHTML
}

function setBusy(busy) {
  for (const id of ['go', 'doc', 'upload-btn', 'delete-doc', 'delete-version']) $(id).disabled = busy;
  if (!busy) updateButtons();
}

// Nothing to redact or remove without a document / version.
function updateButtons() {
  const noDoc = !currentDoc();
  $('go').disabled = noDoc;
  $('doc').disabled = !documents.length;
  $('delete-doc').disabled = noDoc;
  $('delete-version').disabled = !currentVersion();
}

// ─────────────────────────── redaction ───────────────────────────
async function waitForJob(jobId) {
  const started = Date.now();
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status !== 'running') return job;
    const secs = Math.round((Date.now() - started) / 1000);
    show(`⏳ Working… ${secs}s  (scanned documents take longer: OCR + PII detection)`);
    await new Promise((r) => setTimeout(r, 2000));
  }
}

function describe(redaction) {
  const method = redaction.method === 'ocr' ? 'scanned document → OCR route' : 'text PDF → layout preserved';
  const counts = Object.entries(redaction.counts || {}).map(([k, v]) => `${categoryLabels[k] || k}: ${v}`).join(', ')
    || 'no matches found';
  return `✅ Redacted (${method})\n→ ${redaction.name}\n${counts}`;
}

$('go').onclick = async () => {
  setBusy(true);
  try {
    const { job_id } = await api('/api/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_id: $('doc').value, categories: selectedCategories() }),
    });
    const job = await waitForJob(job_id);
    if (job.status === 'failed') throw new Error(job.error);
    show(describe(job.redaction), 'ok');
    await loadWorkspace({ doc: job.document_id, version: job.redaction.id });
    $('compare-card').scrollIntoView({ behavior: 'smooth' });
  } catch (e) {
    show(`❌ ${e.message}`, 'err');
    await loadWorkspace().catch(() => {});   // e.g. the workspace expired
  } finally {
    setBusy(false);
  }
};

// ─────────────────────────── remove ───────────────────────────
async function remove(url, what) {
  setBusy(true);
  try {
    await api(url, { method: 'DELETE' });
    await loadWorkspace();
    show(`🗑 Removed ${what}.`, 'ok');
  } catch (e) {
    show(`❌ ${e.message}`, 'err');
  } finally {
    setBusy(false);
  }
}

$('delete-doc').onclick = () => {
  const doc = currentDoc();
  if (!doc) return;
  const n = doc.redactions.length;
  const what = n ? `${doc.name} and its ${n} redacted version${n > 1 ? 's' : ''}` : doc.name;
  if (confirm(`Remove ${what}? Download anything you need first.`)) {
    remove(`/api/documents/${encodeURIComponent(doc.id)}`, what);
  }
};

$('delete-version').onclick = () => {
  const doc = currentDoc(), version = currentVersion();
  if (version && confirm(`Remove ${version.name}? Download it first if you need it.`)) {
    remove(`/api/documents/${encodeURIComponent(doc.id)}/redactions/${encodeURIComponent(version.id)}`, version.name);
  }
};

// ─────────────────────────── upload ───────────────────────────
$('upload-btn').onclick = () => $('upload').click();
$('upload').onchange = async () => {
  const files = [...$('upload').files];
  $('upload').value = '';   // allow choosing the same file again later
  if (!files.length) return;
  setBusy(true);
  const lines = [];
  let last = null, failed = false;
  try {
    for (const file of files) {
      show(`⏳ Uploading ${file.name}…`);
      const form = new FormData();
      form.append('file', file);
      try {
        const res = await api('/api/documents', { method: 'POST', body: form });
        last = res.id;
        lines.push(res.renamed ? `✅ Uploaded ${file.name} as ${res.name} (that name was taken)` : `✅ Uploaded ${res.name}`);
      } catch (e) {
        failed = true;
        lines.push(`❌ ${e.message}`);
      }
    }
    await loadWorkspace(last ? { doc: last } : {});
  } catch (e) {
    failed = true;
    lines.push(`❌ ${e.message}`);
  } finally {
    show(lines.join('\n'), failed ? 'err' : 'ok');
    setBusy(false);
  }
};
$('select-all').onclick = () => document.querySelectorAll('#cats input').forEach((i) => (i.checked = true));
$('select-none').onclick = () => document.querySelectorAll('#cats input').forEach((i) => (i.checked = false));

// ─────────────────────────── side-by-side comparison ───────────────────────────
// Pages are rendered server-side to PNG and laid out in rows (original | redacted),
// so page N of each document is always aligned and scrolling stays in sync.
let renderToken = 0;

async function renderCompare() {
  const token = ++renderToken;
  const doc = currentDoc(), version = currentVersion();
  $('version-info').textContent = describeVersion(version);
  const container = $('compare');
  container.replaceChildren();

  for (const [kind, which] of [['original', doc && 'original'], ['redacted', version?.id]]) {
    $(`actions-${kind}`).hidden = !which;
    if (which) {
      $(`open-${kind}`).href = fileUrl(doc.id, which);
      $(`download-${kind}`).href = `${fileUrl(doc.id, which)}?download=true`;
    }
  }
  $('cmp-original').textContent = doc?.name || '—';
  if (!doc) {
    $('compare-status').textContent = 'Upload a PDF to start.';
    return;
  }

  $('compare-status').textContent = 'Loading…';
  const info = async (which) => (which ? api(fileUrl(doc.id, which, 'info')) : { pages: 0 });
  let left, right;
  try {
    [left, right] = await Promise.all([info('original'), info(version?.id)]);
  } catch (e) {
    $('compare-status').textContent = `❌ ${e.message}`;
    return;
  }
  if (token !== renderToken) return;   // a newer selection superseded this one

  const pages = Math.max(left.pages, right.pages);
  $('compare-status').textContent = !version
    ? 'This document has not been redacted yet. Run a redaction above to compare.'
    : `${pages} page(s)` + (left.pages !== right.pages ? ' ⚠ page counts differ' : '');

  const titles = document.createElement('div');
  titles.className = 'col-titles';
  titles.append(Object.assign(document.createElement('div'), { textContent: `Original: ${doc.name}` }),
                Object.assign(document.createElement('div'), { textContent: version ? `Redacted: ${version.name}` : 'Not redacted yet' }));
  container.append(titles);

  const cell = (which, n, total) => {
    const div = document.createElement('div');
    div.className = 'page';
    if (!which || n > total) {
      div.classList.add('missing');
      div.textContent = which ? 'No page' : '';
      return div;
    }
    const img = Object.assign(document.createElement('img'), {
      loading: 'lazy', alt: `${which === 'original' ? 'original' : 'redacted'} page ${n}`,
      src: `${fileUrl(doc.id, which, `page/${n}`)}?dpi=${$('zoom').value > 1 ? 160 : 110}`,
    });
    div.append(img, Object.assign(document.createElement('span'), { className: 'label', textContent: `p. ${n}` }));
    return div;
  };

  for (let n = 1; n <= pages; n++) {
    const row = document.createElement('div');
    row.className = 'compare-row';
    row.append(cell('original', n, left.pages), cell(version?.id, n, right.pages));
    container.append(row);
  }
  applyZoom();
}

function applyZoom() {
  $('compare').style.setProperty('--zoom', $('zoom').value);
}

$('doc').onchange = () => showVersions();
$('cmp-version').onchange = renderCompare;
$('zoom').onchange = () => { applyZoom(); renderCompare(); };

init().catch((e) => show(`❌ Failed to load: ${e.message}`, 'err'));
