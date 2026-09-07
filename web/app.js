/* TermuxFM front end. Vanilla ES2017+, no framework, no external requests.
 *
 * File names from the server are inserted with textContent only -- never
 * innerHTML -- so a file called `<img onerror=...>` is text, not markup.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  path: '',
  entries: [],
  sort: 'name',
  order: 'asc',
  selected: new Set(),
  lastIndex: null,
  clipboard: null,        // {op: 'copy'|'move', paths: [], labels: []}
  csrf: null,
  user: null,
  maxUpload: 0,
  searchMode: false,
  sizes: new Map(),       // path -> recursive byte size
};

/* ── helpers ─────────────────────────────────────────────────────── */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  if (bytes < 1024) return bytes + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return (value >= 100 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[index];
}

function formatDate(epoch) {
  if (!epoch) return '';
  const date = new Date(epoch * 1000);
  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  const opts = sameYear
    ? { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: '2-digit' };
  return date.toLocaleString(undefined, opts);
}

const ICONS = {
  dir: '📁', video: '🎬', audio: '🎵', image: '🖼️', text: '📄',
  archive: '🗜️', document: '📕', file: '📦',
};

const KIND_LABELS = {
  dir: 'Folder', video: 'Video', audio: 'Audio', image: 'Image',
  text: 'Text', archive: 'Archive', document: 'Document', file: 'File',
};

function joinPath(base, name) {
  return base ? base + '/' + name : name;
}

function parentPath(path) {
  const index = path.lastIndexOf('/');
  return index < 0 ? '' : path.slice(0, index);
}

function baseName(path) {
  const index = path.lastIndexOf('/');
  return index < 0 ? path : path.slice(index + 1);
}

function q(path) {
  return encodeURIComponent(path);
}

/* ── API ─────────────────────────────────────────────────────────── */

class ApiError extends Error {
  constructor(status, payload) {
    super((payload && payload.message) || ('HTTP ' + status));
    this.status = status;
    this.payload = payload || {};
  }
}

async function api(method, path, body) {
  const options = {
    method,
    credentials: 'same-origin',
    headers: {},
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  if (state.csrf && method !== 'GET' && method !== 'HEAD') {
    options.headers['X-CSRF-Token'] = state.csrf;
  }
  const response = await fetch(path, options);
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch (err) { payload = { message: text }; }
  }
  if (response.status === 401) {
    showLogin('Your session ended. Please sign in again.');
    throw new ApiError(401, payload);
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

/* ── modals ──────────────────────────────────────────────────────── */

let modalResolve = null;

function closeModal(value) {
  $('modal').hidden = true;
  $('modal-body').textContent = '';
  const resolve = modalResolve;
  modalResolve = null;
  if (resolve) resolve(value);
}

function openModal({ title, build, okLabel = 'OK', danger = false, validate }) {
  const modal = $('modal');
  const body = $('modal-body');
  $('modal-title').textContent = title;
  body.textContent = '';
  const ok = $('modal-ok');
  ok.textContent = okLabel;
  ok.className = danger ? 'btn btn-danger' : 'btn btn-primary';
  const context = build(body);
  modal.hidden = false;
  const focusTarget = body.querySelector('input') || ok;
  setTimeout(() => focusTarget.focus(), 20);

  const check = () => { ok.disabled = validate ? !validate(context) : false; };
  check();
  body.addEventListener('input', check);

  return new Promise((resolve) => {
    modalResolve = resolve;
    ok.onclick = () => {
      if (ok.disabled) return;
      closeModal(context.value ? context.value() : true);
    };
  });
}

function promptModal(title, label, initial = '', okLabel = 'OK') {
  return openModal({
    title,
    okLabel,
    build(body) {
      body.appendChild(el('div', null, label));
      const input = el('input');
      input.value = initial;
      input.spellcheck = false;
      input.autocapitalize = 'none';
      body.appendChild(input);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          if (!$('modal-ok').disabled) closeModal(input.value.trim());
        }
      });
      // Preselect the stem so an extension is not accidentally overwritten.
      setTimeout(() => {
        const dot = initial.lastIndexOf('.');
        input.setSelectionRange(0, dot > 0 ? dot : initial.length);
      }, 30);
      return { value: () => input.value.trim() };
    },
    validate: (ctx) => ctx.value().length > 0,
  });
}

function messageModal(title, lines, okLabel = 'Close') {
  return openModal({
    title,
    okLabel,
    build(body) {
      (Array.isArray(lines) ? lines : [lines]).forEach((line) => {
        body.appendChild(el('p', null, line));
      });
      return {};
    },
  });
}

function choiceModal(title, lines, choices) {
  return openModal({
    title,
    okLabel: choices[0].label,
    build(body) {
      (Array.isArray(lines) ? lines : [lines]).forEach((line) => {
        body.appendChild(el('p', null, line));
      });
      const row = el('div', 'modal-actions');
      choices.slice(1).forEach((choice) => {
        const button = el('button', 'btn', choice.label);
        button.onclick = () => closeModal(choice.value);
        row.appendChild(button);
      });
      body.appendChild(row);
      return { value: () => choices[0].value };
    },
  });
}

$('modal-cancel').onclick = () => closeModal(null);
$('modal').addEventListener('click', (event) => {
  if (event.target === $('modal')) closeModal(null);
});

/* ── login ───────────────────────────────────────────────────────── */

function showLogin(message) {
  state.csrf = null;
  $('app').hidden = true;
  $('login-screen').hidden = false;
  const error = $('login-error');
  error.hidden = !message;
  error.textContent = message || '';
  $('login-pass').value = '';
  setTimeout(() => {
    const user = $('login-user');
    (user.value ? $('login-pass') : user).focus();
  }, 30);
}

function showApp(session) {
  state.csrf = session.csrf;
  state.user = session.username;
  state.maxUpload = session.max_upload_bytes || 0;
  $('login-screen').hidden = true;
  $('app').hidden = false;
  $('who').textContent = session.username;
  $('brand').textContent = session.root || 'Server';
  document.title = 'TermuxFM — ' + (session.root || 'Server');
  buildMobileActions();
  navigate('');
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('login-submit');
  button.disabled = true;
  button.textContent = 'Signing in…';
  try {
    const session = await api('POST', '/api/login', {
      username: $('login-user').value,
      password: $('login-pass').value,
    });
    const me = await api('GET', '/api/me');
    showApp(Object.assign({}, session, me));
  } catch (err) {
    const error = $('login-error');
    error.hidden = false;
    error.textContent = err.message || 'Sign in failed';
    $('login-pass').select();
  } finally {
    button.disabled = false;
    button.textContent = 'Sign in';
  }
});

$('btn-logout-mobile').onclick = () => {
  $('toolbar').classList.remove('open');
  $('btn-logout').click();
};

$('btn-logout').onclick = async () => {
  try { await api('POST', '/api/logout'); } catch (err) { /* already gone */ }
  showLogin('You are signed out.');
};

/* ── navigation and rendering ────────────────────────────────────── */

async function navigate(path, { push = true } = {}) {
  try {
    const data = await api(
      'GET',
      '/api/list?path=' + q(path) + '&sort=' + state.sort + '&order=' + state.order
    );
    state.path = data.path;
    state.entries = data.entries;
    state.selected.clear();
    state.lastIndex = null;
    state.searchMode = false;
    $('search-banner').hidden = true;
    $('search-input').value = '';
    if (push) {
      const hash = '#/' + data.path;
      if (location.hash !== hash) history.pushState({ path: data.path }, '', hash);
    }
    render();
  } catch (err) {
    if (err.status === 401) return;
    await messageModal('Could not open folder', err.message);
    if (path !== '') navigate(parentPath(path));
  }
}

window.addEventListener('popstate', () => {
  const path = decodeURIComponent((location.hash || '#/').slice(2));
  navigate(path, { push: false });
});

function renderCrumbs() {
  const crumbs = $('crumbs');
  crumbs.textContent = '';
  const parts = state.path ? state.path.split('/') : [];
  const root = el('button', 'crumb', $('brand').textContent || 'Server');
  root.onclick = () => navigate('');
  if (!parts.length) root.setAttribute('aria-current', 'page');
  crumbs.appendChild(root);
  let accumulated = '';
  parts.forEach((part, index) => {
    accumulated = joinPath(accumulated, part);
    crumbs.appendChild(el('span', 'crumb-sep', '/'));
    const crumb = el('button', 'crumb', part);
    if (index === parts.length - 1) {
      crumb.setAttribute('aria-current', 'page');
    } else {
      const target = accumulated;
      crumb.onclick = () => navigate(target);
    }
    crumbs.appendChild(crumb);
  });
}

function renderSortHeaders() {
  document.querySelectorAll('.sortable').forEach((th) => {
    const field = th.dataset.sort;
    const label = th.textContent.replace(/[↑↓]\s*$/, '').trim();
    th.textContent = field === state.sort
      ? label + ' ' + (state.order === 'asc' ? '↑' : '↓')
      : label;
  });
}

function entryPath(entry) {
  return entry.path !== undefined ? entry.path : joinPath(state.path, entry.name);
}

function render() {
  renderCrumbs();
  renderSortHeaders();
  const rows = $('rows');
  rows.textContent = '';

  state.entries.forEach((entry, index) => {
    const path = entryPath(entry);
    const tr = el('tr');
    tr.dataset.path = path;
    tr.dataset.index = String(index);
    if (state.selected.has(path)) tr.classList.add('selected');
    if (state.clipboard && state.clipboard.op === 'move'
        && state.clipboard.paths.includes(path)) {
      tr.classList.add('cut');
    }

    const check = el('td', 'col-check');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = state.selected.has(path);
    box.setAttribute('aria-label', 'Select ' + entry.name);
    box.onclick = (event) => {
      event.stopPropagation();
      toggle(path, box.checked);
      state.lastIndex = index;
    };
    check.appendChild(box);
    tr.appendChild(check);

    const nameCell = el('td');
    const wrap = el('div', 'cell-name');
    wrap.appendChild(el('span', 'entry-icon', ICONS[entry.kind] || ICONS.file));
    const link = el('button', 'entry-link', entry.name);
    link.title = entry.name;
    link.onclick = (event) => {
      event.stopPropagation();
      openEntry(entry);
    };
    wrap.appendChild(link);
    if (entry.is_link) wrap.appendChild(el('span', 'link-badge', 'link'));
    if (state.searchMode && entry.parent !== undefined) {
      wrap.appendChild(el('span', 'entry-sub', 'in ' + (entry.parent || '/')));
    }
    nameCell.appendChild(wrap);
    tr.appendChild(nameCell);

    const known = state.sizes.get(path);
    const sizeText = entry.is_dir
      ? (known !== undefined ? formatSize(known) : '—')
      : formatSize(entry.size);
    tr.appendChild(el('td', 'cell-size', sizeText));
    tr.appendChild(el('td', 'cell-date', formatDate(entry.mtime)));
    tr.appendChild(el('td', 'cell-kind', KIND_LABELS[entry.kind] || 'File'));

    tr.onclick = (event) => rowClick(event, path, index);
    tr.ondblclick = () => openEntry(entry);

    // Drag a selection onto a folder row to move it there.
    tr.draggable = true;
    tr.addEventListener('dragstart', (event) => {
      if (!state.selected.has(path)) selectOnly(path, index);
      event.dataTransfer.setData('application/x-termuxfm',
                                 JSON.stringify([...state.selected]));
      event.dataTransfer.effectAllowed = 'move';
    });
    if (entry.is_dir) {
      tr.addEventListener('dragover', (event) => {
        if (event.dataTransfer.types.includes('application/x-termuxfm')) {
          event.preventDefault();
          tr.classList.add('drop-target');
        }
      });
      tr.addEventListener('dragleave', () => tr.classList.remove('drop-target'));
      tr.addEventListener('drop', async (event) => {
        tr.classList.remove('drop-target');
        const raw = event.dataTransfer.getData('application/x-termuxfm');
        if (!raw) return;
        event.preventDefault();
        event.stopPropagation();
        const paths = JSON.parse(raw).filter((p) => p !== path);
        if (paths.length) await transfer('move', paths, path);
      });
    }

    rows.appendChild(tr);
  });

  $('empty-note').hidden = state.entries.length > 0;
  $('empty-note').textContent = state.searchMode
    ? 'No matches.' : 'This folder is empty.';
  updateStatus();
  updateToolbar();
}

function updateStatus() {
  const folders = state.entries.filter((e) => e.is_dir).length;
  const files = state.entries.length - folders;
  const bytes = state.entries.reduce((sum, e) => sum + (e.is_dir ? 0 : e.size), 0);
  const parts = [];
  if (folders) parts.push(folders + (folders === 1 ? ' folder' : ' folders'));
  if (files) parts.push(files + (files === 1 ? ' file' : ' files'));
  if (files) parts.push(formatSize(bytes));
  if (state.selected.size) parts.push(state.selected.size + ' selected');
  $('status-line').textContent = parts.join(' · ');
}

function updateToolbar() {
  const count = state.selected.size;
  const single = count === 1;
  const selectedEntries = state.entries.filter(
    (e) => state.selected.has(entryPath(e)));
  const onlyFiles = count > 0 && selectedEntries.every((e) => !e.is_dir);
  const anyDir = selectedEntries.some((e) => e.is_dir);

  $('btn-copy').disabled = count === 0;
  $('btn-cut').disabled = count === 0;
  $('btn-rename').disabled = !single;
  $('btn-download').disabled = !(count === 1 && onlyFiles);
  $('btn-zip').disabled = !single;
  $('btn-size').disabled = !(single && anyDir);
  $('btn-delete').disabled = count === 0;

  const paste = $('btn-paste');
  const cancel = $('btn-paste-cancel');
  if (state.clipboard) {
    paste.hidden = false;
    cancel.hidden = false;
    const verb = state.clipboard.op === 'copy' ? 'Paste' : 'Move here';
    paste.textContent = verb + ' (' + state.clipboard.paths.length + ')';
  } else {
    paste.hidden = true;
    cancel.hidden = true;
  }
  const all = $('check-all');
  all.checked = count > 0 && count === state.entries.length;
  all.indeterminate = count > 0 && count < state.entries.length;
  buildMobileActions();
}

/* Mirror the enabled toolbar buttons into the mobile bottom bar. */
function buildMobileActions() {
  const bar = $('mobile-actions');
  if (!bar) return;
  bar.textContent = '';
  const ids = ['btn-paste', 'btn-rename', 'btn-download', 'btn-zip',
               'btn-delete'];
  let shown = 0;
  ids.forEach((id) => {
    const source = $(id);
    if (!source || source.disabled || source.hidden) return;
    const clone = el('button', source.className, source.textContent);
    clone.onclick = () => source.click();
    bar.appendChild(clone);
    shown += 1;
  });
  bar.hidden = shown === 0;
}

/* ── selection ───────────────────────────────────────────────────── */

function toggle(path, on) {
  if (on) state.selected.add(path); else state.selected.delete(path);
  syncRowClasses();
  updateStatus();
  updateToolbar();
}

function selectOnly(path, index) {
  state.selected.clear();
  state.selected.add(path);
  state.lastIndex = index;
  syncRowClasses();
  updateStatus();
  updateToolbar();
}

function syncRowClasses() {
  document.querySelectorAll('#rows tr').forEach((tr) => {
    const on = state.selected.has(tr.dataset.path);
    tr.classList.toggle('selected', on);
    const box = tr.querySelector('input[type=checkbox]');
    if (box) box.checked = on;
  });
}

function rowClick(event, path, index) {
  if (event.shiftKey && state.lastIndex !== null) {
    const [from, to] = [state.lastIndex, index].sort((a, b) => a - b);
    for (let i = from; i <= to; i += 1) {
      state.selected.add(entryPath(state.entries[i]));
    }
    syncRowClasses();
    updateStatus();
    updateToolbar();
  } else if (event.ctrlKey || event.metaKey) {
    toggle(path, !state.selected.has(path));
    state.lastIndex = index;
  } else {
    selectOnly(path, index);
  }
}

$('check-all').onclick = (event) => {
  state.selected.clear();
  if (event.target.checked) {
    state.entries.forEach((entry) => state.selected.add(entryPath(entry)));
  }
  syncRowClasses();
  updateStatus();
  updateToolbar();
};

document.querySelectorAll('.sortable').forEach((th) => {
  th.onclick = () => {
    const field = th.dataset.sort;
    if (state.sort === field) {
      state.order = state.order === 'asc' ? 'desc' : 'asc';
    } else {
      state.sort = field;
      state.order = 'asc';
    }
    if (state.searchMode) {
      render();
    } else {
      navigate(state.path, { push: false });
    }
  };
});

/* ── opening entries ─────────────────────────────────────────────── */

const PREVIEWABLE = new Set(['image', 'video', 'audio', 'text']);

function openEntry(entry) {
  const path = entryPath(entry);
  if (entry.is_dir) {
    navigate(path);
    return;
  }
  if (PREVIEWABLE.has(entry.kind)) {
    openViewer(entry, path);
    return;
  }
  download(path);
}

function download(path) {
  // A hidden anchor keeps the streaming download out of the SPA's control.
  const anchor = el('a');
  anchor.href = '/api/download?path=' + q(path);
  anchor.download = baseName(path);
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

async function openViewer(entry, path) {
  const viewer = $('viewer');
  const body = $('viewer-body');
  body.textContent = '';
  $('viewer-name').textContent = entry.name;
  const link = $('viewer-download');
  link.href = '/api/download?path=' + q(path);
  link.setAttribute('download', entry.name);
  viewer.hidden = false;

  const source = '/api/preview?path=' + q(path);
  if (entry.kind === 'image') {
    const img = el('img');
    img.alt = entry.name;
    img.src = source;
    img.onerror = () => {
      body.textContent = '';
      body.appendChild(el('p', 'viewer-note', 'This image cannot be displayed.'));
    };
    body.appendChild(img);
  } else if (entry.kind === 'video' || entry.kind === 'audio') {
    const media = el(entry.kind === 'video' ? 'video' : 'audio');
    media.controls = true;
    media.preload = 'metadata';
    media.src = source;   // Range requests make seeking work server-side.
    media.onerror = () => {
      body.textContent = '';
      body.appendChild(el('p', 'viewer-note',
        'Your browser cannot play this file. Use Download instead.'));
    };
    body.appendChild(media);
  } else {
    body.appendChild(el('p', 'viewer-note', 'Loading…'));
    try {
      const response = await fetch(source, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const text = await response.text();
      body.textContent = '';
      body.appendChild(el('pre', null, text));
      if (response.headers.get('X-Preview-Truncated') === '1') {
        body.appendChild(el('p', 'viewer-note',
          'Preview truncated to the first 1 MB.'));
      }
    } catch (err) {
      body.textContent = '';
      body.appendChild(el('p', 'viewer-note', 'Could not load preview.'));
    }
  }
}

function closeViewer() {
  const body = $('viewer-body');
  body.querySelectorAll('video, audio').forEach((media) => {
    media.pause();
    media.removeAttribute('src');
    media.load();
  });
  body.textContent = '';
  $('viewer').hidden = true;
}

$('viewer-close').onclick = closeViewer;

/* ── jobs ────────────────────────────────────────────────────────── */

const jobCards = new Map();

function jobCard(job) {
  let card = jobCards.get(job.id);
  if (!card) {
    const root = el('div', 'job');
    const head = el('div', 'job-head');
    const label = el('span', 'job-label', job.label);
    const cancel = el('button', 'icon-btn', '✕');
    cancel.title = 'Cancel';
    cancel.onclick = () => api('POST', '/api/job/cancel', { id: job.id })
      .catch(() => {});
    head.appendChild(label);
    head.appendChild(cancel);
    const detail = el('div', 'job-detail');
    const bar = el('div', 'progress');
    const fill = el('div', 'progress-fill');
    bar.appendChild(fill);
    root.appendChild(head);
    root.appendChild(detail);
    root.appendChild(bar);
    $('jobs').appendChild(root);
    card = { root, detail, fill, cancel };
    jobCards.set(job.id, card);
  }
  return card;
}

function describeJob(job) {
  if (job.state === 'pending') return 'Queued…';
  if (job.total_bytes) {
    return formatSize(job.done_bytes) + ' of ' + formatSize(job.total_bytes)
      + (job.current ? ' · ' + job.current : '');
  }
  if (job.total_items) {
    return job.done_items + ' of ' + job.total_items + ' items'
      + (job.current ? ' · ' + job.current : '');
  }
  return job.current || 'Working…';
}

function jobProgress(job) {
  if (job.total_bytes) return Math.min(100, job.done_bytes / job.total_bytes * 100);
  if (job.total_items) return Math.min(100, job.done_items / job.total_items * 100);
  return job.done ? 100 : 5;
}

/** Poll a job to completion, updating its card. Resolves with the snapshot. */
function trackJob(job, { silent = false } = {}) {
  return new Promise((resolve) => {
    const card = silent ? null : jobCard(job);
    let delay = 120;
    const tick = async () => {
      let snapshot;
      try {
        snapshot = (await api('GET', '/api/job?id=' + job.id)).job;
      } catch (err) {
        if (card) card.root.remove();
        jobCards.delete(job.id);
        resolve(null);
        return;
      }
      if (card) {
        card.detail.textContent = describeJob(snapshot);
        card.fill.style.width = jobProgress(snapshot) + '%';
      }
      if (!snapshot.done) {
        delay = Math.min(delay * 1.15, 1000);
        setTimeout(tick, delay);
        return;
      }
      if (card) {
        card.cancel.remove();
        if (snapshot.state === 'error' || snapshot.error_count) {
          card.detail.classList.add('job-error');
          card.detail.textContent = snapshot.state === 'error'
            ? snapshot.message
            : snapshot.error_count + ' problem(s)';
          setTimeout(() => { card.root.remove(); jobCards.delete(job.id); }, 6000);
        } else {
          card.root.remove();
          jobCards.delete(job.id);
        }
      }
      resolve(snapshot);
    };
    setTimeout(tick, 60);
  });
}

async function runJob(method, path, body, options) {
  const started = await api(method, path, body);
  return trackJob(started.job, options);
}

async function reportJobProblems(snapshot, title) {
  if (!snapshot) return;
  if (snapshot.state === 'error') {
    await messageModal(title, snapshot.message || 'The operation failed.');
    return;
  }
  if (snapshot.state === 'cancelled') return;
  if (snapshot.error_count) {
    const lines = snapshot.errors.slice(0, 8).map(
      (e) => baseName(e.path) + ': ' + e.message);
    if (snapshot.error_count > lines.length) {
      lines.push('…and ' + (snapshot.error_count - lines.length) + ' more.');
    }
    await messageModal(title, lines);
  }
}

/* ── operations ──────────────────────────────────────────────────── */

$('btn-refresh').onclick = () => {
  state.sizes.clear();
  navigate(state.path, { push: false });
};

$('btn-mkdir').onclick = async () => {
  const name = await promptModal('New folder', 'Folder name', '', 'Create');
  if (!name) return;
  try {
    await api('POST', '/api/mkdir', { path: state.path, name });
    navigate(state.path, { push: false });
  } catch (err) {
    await messageModal('Could not create folder', err.message);
  }
};

$('btn-rename').onclick = async () => {
  const path = [...state.selected][0];
  if (!path) return;
  const name = await promptModal('Rename', 'New name', baseName(path), 'Rename');
  if (!name || name === baseName(path)) return;
  try {
    await api('POST', '/api/rename', { path, name });
    navigate(state.path, { push: false });
  } catch (err) {
    await messageModal('Could not rename', err.message);
  }
};

$('btn-download').onclick = () => {
  const path = [...state.selected][0];
  if (path) download(path);
};

$('btn-zip').onclick = () => {
  const path = [...state.selected][0];
  if (!path) return;
  const anchor = el('a');
  anchor.href = '/api/zip?path=' + q(path);
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
};

$('btn-size').onclick = async () => {
  const path = [...state.selected][0];
  if (!path) return;
  const snapshot = await runJob('POST', '/api/du', { path });
  if (snapshot && snapshot.state === 'done') {
    state.sizes.set(path, snapshot.result.bytes);
    render();
    await messageModal(baseName(path) || 'Root', [
      formatSize(snapshot.result.bytes),
      snapshot.result.files + ' files in ' + snapshot.result.dirs + ' folders',
    ]);
  }
};

$('btn-copy').onclick = () => setClipboard('copy');
$('btn-cut').onclick = () => setClipboard('move');
$('btn-paste-cancel').onclick = () => { state.clipboard = null; render(); };

function setClipboard(op) {
  if (!state.selected.size) return;
  state.clipboard = { op, paths: [...state.selected] };
  render();
}

$('btn-paste').onclick = async () => {
  if (!state.clipboard) return;
  const { op, paths } = state.clipboard;
  state.clipboard = null;
  await transfer(op, paths, state.path);
};

async function transfer(op, paths, dest, conflict = 'fail') {
  const title = op === 'copy' ? 'Copy' : 'Move';
  let snapshot;
  try {
    snapshot = await runJob('POST', '/api/' + op,
                            { paths, dest, conflict });
  } catch (err) {
    await messageModal('Could not ' + op, err.message);
    return;
  }
  navigate(state.path, { push: false });
  if (!snapshot) return;

  // Every failure was a name collision: offer the two sensible ways out
  // rather than making the user rename things by hand.
  const collisions = snapshot.errors.filter(
    (e) => /already exists/i.test(e.message));
  if (snapshot.error_count && collisions.length === snapshot.error_count) {
    const choice = await choiceModal(
      title + ': name conflicts',
      [collisions.length + ' item(s) already exist in the destination.'],
      [
        { label: 'Keep both', value: 'rename' },
        { label: 'Overwrite', value: 'overwrite' },
        { label: 'Skip', value: null },
      ]);
    if (choice) {
      const retry = collisions.map((e) => e.path)
        .map((abs) => paths.find((p) => abs.endsWith(baseName(p))) || null)
        .filter(Boolean);
      await transfer(op, retry.length ? retry : paths, dest, choice);
    }
    return;
  }
  await reportJobProblems(snapshot, title + ' finished with problems');
}

$('btn-delete').onclick = async () => {
  const paths = [...state.selected];
  if (!paths.length) return;
  const entries = state.entries.filter((e) => paths.includes(entryPath(e)));
  const folders = entries.filter((e) => e.is_dir);

  const confirmed = await openModal({
    title: 'Delete permanently',
    okLabel: 'Delete',
    danger: true,
    build(body) {
      body.appendChild(el('p', null,
        'These ' + paths.length + ' item(s) will be deleted:'));
      const list = el('ul');
      entries.slice(0, 12).forEach((entry) => {
        list.appendChild(el('li', null,
          entry.name + (entry.is_dir ? '  (folder and everything in it)' : '')));
      });
      if (entries.length > 12) {
        list.appendChild(el('li', null,
          '…and ' + (entries.length - 12) + ' more'));
      }
      body.appendChild(list);
      body.appendChild(el('p', 'modal-warn',
        'There is no trash. This cannot be undone.'));
      // Deleting a folder is the expensive mistake, so it costs a keystroke.
      if (folders.length === 1) {
        body.appendChild(el('div', null,
          'Type the folder name to confirm:'));
        const input = el('input');
        input.placeholder = folders[0].name;
        input.spellcheck = false;
        input.autocapitalize = 'none';
        body.appendChild(input);
        return { value: () => true, input, expect: folders[0].name };
      }
      if (folders.length > 1) {
        body.appendChild(el('div', null, 'Type DELETE to confirm:'));
        const input = el('input');
        input.placeholder = 'DELETE';
        input.spellcheck = false;
        body.appendChild(input);
        return { value: () => true, input, expect: 'DELETE' };
      }
      return { value: () => true };
    },
    validate: (ctx) => !ctx.input || ctx.input.value.trim() === ctx.expect,
  });
  if (!confirmed) return;

  const snapshot = await runJob('POST', '/api/delete',
                                { paths, confirm: true });
  state.sizes.clear();
  navigate(state.path, { push: false });
  await reportJobProblems(snapshot, 'Delete finished with problems');
};

/* ── search ──────────────────────────────────────────────────────── */

$('search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = $('search-input').value.trim();
  if (!query) return;
  const snapshot = await runJob('POST', '/api/search',
                                { path: state.path, query });
  if (!snapshot || snapshot.state !== 'done') {
    if (snapshot && snapshot.state === 'error') {
      await messageModal('Search failed', snapshot.message);
    }
    return;
  }
  const result = snapshot.result;
  state.entries = result.entries;
  state.selected.clear();
  state.lastIndex = null;
  state.searchMode = true;
  $('search-banner').hidden = false;
  $('search-banner-text').textContent =
    result.entries.length + ' match(es) for “' + result.query + '” in '
    + (result.base || 'Server') + (result.truncated ? ' (truncated)' : '');
  render();
});

$('btn-clear-search').onclick = () => navigate(state.path, { push: false });

/* ── uploads ─────────────────────────────────────────────────────── */

const uploadQueue = [];
let activeUploads = 0;
const MAX_PARALLEL_UPLOADS = 2;
let uploadStats = { total: 0, done: 0, bytesTotal: 0, bytesDone: 0 };

function b64url(text) {
  // btoa needs latin1, so encode UTF-8 by hand first.
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function uploadRow(item) {
  const li = el('li');
  const name = el('span', 'u-name', item.relPath || item.file.name);
  const stateSpan = el('span', 'u-state', 'waiting');
  li.appendChild(name);
  li.appendChild(stateSpan);
  $('uploads-list').appendChild(li);
  item.row = li;
  item.stateSpan = stateSpan;
}

function refreshUploadPanel() {
  const panel = $('uploads');
  panel.hidden = uploadStats.total === 0;
  const percent = uploadStats.bytesTotal
    ? uploadStats.bytesDone / uploadStats.bytesTotal * 100 : 0;
  $('uploads-bar').style.width = Math.min(100, percent) + '%';
  const remaining = uploadStats.total - uploadStats.done;
  $('uploads-title').textContent = remaining
    ? 'Uploading ' + remaining + ' file(s)'
    : 'Uploaded ' + uploadStats.done + ' file(s)';
}

$('uploads-close').onclick = () => {
  $('uploads').hidden = true;
  $('uploads-list').textContent = '';
  uploadStats = { total: 0, done: 0, bytesTotal: 0, bytesDone: 0 };
};

function enqueueUploads(files, dest) {
  const accepted = [];
  files.forEach((entry) => {
    const file = entry.file || entry;
    const relPath = entry.relPath
      || (file.webkitRelativePath ? file.webkitRelativePath : '');
    if (state.maxUpload && file.size > state.maxUpload) {
      const item = { file, relPath, dest, tooBig: true };
      uploadRow(item);
      item.stateSpan.textContent = 'too large';
      item.stateSpan.classList.add('u-error');
      uploadStats.total += 1;
      uploadStats.done += 1;
      return;
    }
    accepted.push({ file, relPath, dest });
  });
  accepted.forEach((item) => {
    uploadRow(item);
    uploadQueue.push(item);
    uploadStats.total += 1;
    uploadStats.bytesTotal += item.file.size;
  });
  refreshUploadPanel();
  pumpUploads();
}

function pumpUploads() {
  while (activeUploads < MAX_PARALLEL_UPLOADS && uploadQueue.length) {
    const item = uploadQueue.shift();
    activeUploads += 1;
    startUpload(item);
  }
  if (!activeUploads && !uploadQueue.length) {
    state.sizes.clear();
    navigate(state.path, { push: false });
  }
}

function startUpload(item) {
  // XHR, not fetch: only XHR reports upload progress in current browsers.
  const xhr = new XMLHttpRequest();
  const url = '/api/upload?dir=' + q(item.dest) + '&conflict=rename';
  let counted = 0;
  xhr.open('PUT', url, true);
  xhr.setRequestHeader('X-CSRF-Token', state.csrf);
  xhr.setRequestHeader('X-Filename', b64url(item.file.name));
  if (item.relPath) xhr.setRequestHeader('X-Rel-Path', b64url(item.relPath));
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    uploadStats.bytesDone += event.loaded - counted;
    counted = event.loaded;
    const percent = Math.round(event.loaded / event.total * 100);
    item.stateSpan.textContent = percent + '%';
    refreshUploadPanel();
  };
  xhr.onload = () => {
    uploadStats.done += 1;
    if (xhr.status >= 200 && xhr.status < 300) {
      item.stateSpan.textContent = 'done';
      item.stateSpan.classList.add('u-done');
    } else {
      let message = 'failed (' + xhr.status + ')';
      try {
        const payload = JSON.parse(xhr.responseText);
        if (payload.message) message = payload.message;
      } catch (err) { /* keep the status-code message */ }
      item.stateSpan.textContent = message;
      item.stateSpan.classList.add('u-error');
      item.stateSpan.title = message;
    }
    activeUploads -= 1;
    refreshUploadPanel();
    pumpUploads();
  };
  xhr.onerror = () => {
    uploadStats.done += 1;
    item.stateSpan.textContent = 'network error';
    item.stateSpan.classList.add('u-error');
    activeUploads -= 1;
    refreshUploadPanel();
    pumpUploads();
  };
  xhr.send(item.file);
}

$('btn-upload').onclick = () => $('file-input').click();
$('btn-upload-dir').onclick = () => $('dir-input').click();

$('file-input').onchange = (event) => {
  enqueueUploads([...event.target.files], state.path);
  event.target.value = '';
};
$('dir-input').onchange = (event) => {
  const files = [...event.target.files].map((file) => ({
    file, relPath: file.webkitRelativePath || file.name,
  }));
  enqueueUploads(files, state.path);
  event.target.value = '';
};

/* ── drag and drop upload ────────────────────────────────────────── */

/** Walk a dropped directory entry, collecting files with their rel paths. */
function walkEntry(entry, prefix, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => {
        out.push({ file, relPath: prefix + file.name });
        resolve();
      }, resolve);
      return;
    }
    if (!entry.isDirectory) { resolve(); return; }
    const reader = entry.createReader();
    const children = [];
    const readBatch = () => {
      reader.readEntries(async (batch) => {
        if (!batch.length) {
          for (const child of children) {
            await walkEntry(child, prefix + entry.name + '/', out);
          }
          resolve();
          return;
        }
        children.push(...batch);
        readBatch();          // readEntries returns at most 100 at a time
      }, resolve);
    };
    readBatch();
  });
}

const dropzone = $('dropzone');
let dragDepth = 0;

function isFileDrag(event) {
  const types = event.dataTransfer ? event.dataTransfer.types : null;
  return types && [...types].includes('Files');
}

dropzone.addEventListener('dragenter', (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  dragDepth += 1;
  dropzone.classList.add('dragging');
  $('drop-hint').hidden = false;
});
dropzone.addEventListener('dragover', (event) => {
  if (isFileDrag(event)) event.preventDefault();
});
dropzone.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) {
    dropzone.classList.remove('dragging');
    $('drop-hint').hidden = true;
  }
});
dropzone.addEventListener('drop', async (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  dragDepth = 0;
  dropzone.classList.remove('dragging');
  $('drop-hint').hidden = true;
  if (state.searchMode) {
    await messageModal('Cannot upload here',
      'Leave search results first, then upload into a folder.');
    return;
  }

  const items = event.dataTransfer.items;
  const collected = [];
  if (items && items.length && items[0].webkitGetAsEntry) {
    const entries = [...items]
      .map((item) => item.webkitGetAsEntry())
      .filter(Boolean);
    for (const entry of entries) await walkEntry(entry, '', collected);
  } else {
    [...event.dataTransfer.files].forEach((file) => collected.push({ file }));
  }
  if (collected.length) enqueueUploads(collected, state.path);
});

/* ── keyboard, theme, menu ───────────────────────────────────────── */

document.addEventListener('keydown', (event) => {
  if (!$('viewer').hidden) {
    if (event.key === 'Escape') closeViewer();
    return;
  }
  if (!$('modal').hidden) {
    if (event.key === 'Escape') closeModal(null);
    return;
  }
  if ($('app').hidden) return;
  const tag = document.activeElement ? document.activeElement.tagName : '';
  const typing = tag === 'INPUT' || tag === 'TEXTAREA';

  if (event.key === '/' && !typing) {
    event.preventDefault();
    $('search-input').focus();
    return;
  }
  if (typing) {
    if (event.key === 'Escape') document.activeElement.blur();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
    event.preventDefault();
    state.entries.forEach((entry) => state.selected.add(entryPath(entry)));
    syncRowClasses();
    updateStatus();
    updateToolbar();
    return;
  }
  if (event.key === 'F2' && state.selected.size === 1) $('btn-rename').click();
  if (event.key === 'Delete' && state.selected.size) $('btn-delete').click();
  if (event.key === 'Escape') {
    state.selected.clear();
    syncRowClasses();
    updateStatus();
    updateToolbar();
  }
  if (event.key === 'Backspace' && state.path) navigate(parentPath(state.path));
});

const THEME_KEY = 'termuxfm-theme';
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch (err) { /* private mode */ }
}
$('btn-theme').onclick = () => {
  const order = ['auto', 'light', 'dark'];
  const current = document.documentElement.dataset.theme || 'auto';
  applyTheme(order[(order.indexOf(current) + 1) % order.length]);
};
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) document.documentElement.dataset.theme = saved;
} catch (err) { /* private mode */ }

$('btn-menu').onclick = () => $('toolbar').classList.toggle('open');

/* ── boot ────────────────────────────────────────────────────────── */

(async function boot() {
  try {
    const me = await api('GET', '/api/me');
    showApp(me);
    const hash = decodeURIComponent((location.hash || '#/').slice(2));
    if (hash) navigate(hash, { push: false });
  } catch (err) {
    showLogin(null);
  }
})();
