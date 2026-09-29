'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

let state = { speakers: [] };
let sources = [];

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const r = await fetch('/api' + path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    let msg = data.detail || r.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join('; ');
    throw new Error(msg);
  }
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

async function run(fn, okMsg) {
  try { await fn(); if (okMsg) toast(okMsg); } catch (e) { toast(e.message, true); }
}

// ---- navigation ---------------------------------------------------------------------------

function route() {
  const page = (location.hash || '#speakers').slice(1);
  document.querySelectorAll('[data-page]').forEach((el) => (el.hidden = el.dataset.page !== page));
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.hash === '#' + page));
  ({ schedules: loadSchedules, library: loadSources, activity: loadActivity, soundboard: loadSources }[page] || (() => {}))();
}
window.addEventListener('hashchange', route);

$('#theme').onclick = () => {
  const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('theme', t); } catch (e) {}
};

// ---- speakers -----------------------------------------------------------------------------

function speakerOptions(sel, withAll) {
  const cur = sel.value;
  sel.innerHTML = (withAll ? '<option value="*">All speakers</option>' : '') +
    state.speakers.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function musicOptions() {
  const group = (kind, label) => {
    const items = sources.filter((s) => s.kind === kind);
    return items.length ? `<optgroup label="${label}">${items.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}</optgroup>` : '';
  };
  return group('station', 'Stations') + group('playlist', 'Playlists');
}

function renderSpeakers() {
  $('#user').textContent = state.user || '';
  const alert = $('#alert');
  alert.hidden = !state.speaker_error;
  alert.textContent = state.speaker_error ? `Can't reach Protect: ${state.speaker_error}` : '';
  ['#sb-target', '#tts-target'].forEach((s) => speakerOptions($(s), true));

  const wrap = $('#speakers');
  if (!state.speakers.length) {
    wrap.innerHTML = '<div class="card muted">No speakers found in Protect.</div>';
    return;
  }
  // Keep the cards (and any open select) stable between polls; only patch the live parts.
  for (const s of state.speakers) {
    let card = wrap.querySelector(`[data-id="${CSS.escape(s.id)}"]`);
    if (!card) {
      card = document.createElement('div');
      card.className = 'card';
      card.dataset.id = s.id;
      card.innerHTML = `
        <div class="card-head"><div class="card-title">${esc(s.name)}</div><span class="badge" data-badge></span></div>
        <div class="stack">
          <div class="now" data-now></div>
          <div class="row">
            <label class="field"><span>Play</span><select data-src></select></label>
            <button class="btn primary" data-play>Play</button>
          </div>
          <div class="row">
            <button class="btn secondary sm" data-skip hidden>Next track</button>
            <button class="btn danger sm" data-stop>Stop</button>
          </div>
        </div>`;
      card.querySelector('[data-play]').onclick = () => run(async () => {
        await api(`/speakers/${encodeURIComponent(s.id)}/play`, { body: { source_id: +card.querySelector('[data-src]').value } });
        await poll();
      });
      card.querySelector('[data-stop]').onclick = () => run(async () => { await api(`/speakers/${encodeURIComponent(s.id)}/stop`, { method: 'POST' }); await poll(); });
      card.querySelector('[data-skip]').onclick = () => run(() => api(`/speakers/${encodeURIComponent(s.id)}/skip`, { method: 'POST' }));
      wrap.querySelector('.muted')?.remove();
      wrap.appendChild(card);
    }
    const sel = card.querySelector('[data-src]');
    if (document.activeElement !== sel) {
      const cur = sel.value;
      sel.innerHTML = musicOptions();
      if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    }
    const badge = card.querySelector('[data-badge]');
    const offline = s.state && s.state !== 'CONNECTED';
    badge.className = 'badge ' + (s.error || offline ? 'warning' : s.playing ? 'accent' : '');
    badge.textContent = s.error ? 'Error' : offline ? s.state.toLowerCase() : s.playing ? 'Playing' : 'Idle';
    card.classList.toggle('live', !!s.playing && !s.error);
    card.classList.toggle('bad', !!s.error);
    const origin = s.music && s.music.origin !== 'manual' ? ' <span class="muted">(scheduled)</span>' : '';
    card.querySelector('[data-now]').innerHTML = s.error
      ? `<span style="color:var(--color-warning)">${esc(s.error)}</span>`
      : s.playing
        ? `${esc(s.playing)}${origin}${s.title ? `<div class="title">♪ ${esc(s.title)}</div>` : ''}`
        : '<span class="muted">Nothing playing</span>';
    card.querySelector('[data-skip]').hidden = !(s.music && s.music.kind === 'playlist');
  }
  wrap.querySelectorAll('[data-id]').forEach((c) => {
    if (!state.speakers.some((s) => s.id === c.dataset.id)) c.remove();
  });
}

async function poll() {
  try { state = await api('/state'); renderSpeakers(); } catch (e) { /* keep last state */ }
}

$('#refresh').onclick = () => run(async () => { state = await api('/speakers/refresh', { method: 'POST' }); renderSpeakers(); }, 'Speakers refreshed');

// ---- library ------------------------------------------------------------------------------

async function loadSources() {
  sources = await api('/sources');
  renderLibrary();
  renderPads();
  renderSpeakers();
}

function delBtn(id, label) {
  return `<button class="btn danger sm" data-del="${id}" data-label="${esc(label)}">Delete</button>`;
}

function renderLibrary() {
  const stations = sources.filter((s) => s.kind === 'station');
  $('#stations').innerHTML = stations.map((s) => `<tr><td>${esc(s.name)}</td><td class="mono muted">${esc(s.target)}</td>
    <td class="actions">${delBtn(s.id, s.name)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No stations yet.</td></tr>';

  const clips = sources.filter((s) => s.kind === 'clip');
  $('#clips').innerHTML = clips.map((s) => `<tr><td>${esc(s.name)}</td>
    <td><audio controls preload="none" src="/api/clips/${s.id}/audio" style="height:32px"></audio></td>
    <td class="actions">${delBtn(s.id, s.name)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No clips yet.</td></tr>';

  const lists = sources.filter((s) => s.kind === 'playlist');
  $('#playlists').innerHTML = lists.map((p) => `
    <div class="card">
      <div class="card-head"><div class="card-title">${esc(p.name)}</div><span class="badge">${p.tracks.length} tracks</span></div>
      <div class="table-wrap" style="max-height:240px;overflow-y:auto"><table><tbody>
        ${p.tracks.map((t) => `<tr><td>${esc(t)}</td><td class="actions"><button class="icon-btn" data-deltrack="${p.id}" data-track="${esc(t)}" title="Remove">×</button></td></tr>`).join('') || '<tr><td class="muted">Empty. Upload some music.</td></tr>'}
      </tbody></table></div>
      <div class="row" style="margin-top:12px">
        <label class="field"><span>Add tracks</span><input type="file" accept="audio/*,video/*,.caf,.aif,.aiff,.amr,.3gp,.m4a" multiple data-upload="${p.id}"></label>
      </div>
      <div class="row" style="margin-top:12px">${delBtn(p.id, p.name)}</div>
    </div>`).join('') || '<div class="card muted">No playlists yet.</div>';
}

document.addEventListener('click', (e) => {
  const del = e.target.closest('[data-del]');
  if (del && confirm(`Delete "${del.dataset.label}"?`)) {
    run(async () => { await api(`/sources/${del.dataset.del}`, { method: 'DELETE' }); await loadSources(); }, 'Deleted');
  }
  const dt = e.target.closest('[data-deltrack]');
  if (dt && confirm(`Remove "${dt.dataset.track}"?`)) {
    run(async () => {
      await api(`/playlists/${dt.dataset.deltrack}/tracks/${encodeURIComponent(dt.dataset.track)}`, { method: 'DELETE' });
      await loadSources();
    });
  }
  if (e.target.matches('[data-close]')) $('#modal').hidden = true;
});

document.addEventListener('change', (e) => {
  const up = e.target.closest('[data-upload]');
  if (!up || !up.files.length) return;
  const fd = new FormData();
  [...up.files].forEach((f) => fd.append('files', f));
  toast(`Uploading ${up.files.length} file(s)…`);
  run(async () => { await api(`/playlists/${up.dataset.upload}/tracks`, { body: fd }); await loadSources(); }, 'Uploaded');
});

$('#station-form').onsubmit = (e) => {
  e.preventDefault();
  const f = e.target;
  run(async () => { await api('/stations', { body: { name: f.name.value, url: f.url.value } }); f.reset(); await loadSources(); }, 'Station added');
};
$('#playlist-form').onsubmit = (e) => {
  e.preventDefault();
  const f = e.target;
  run(async () => { await api('/playlists', { body: { name: f.name.value } }); f.reset(); await loadSources(); }, 'Playlist created');
};
$('#clip-form').onsubmit = (e) => {
  e.preventDefault();
  const f = e.target;
  run(async () => { await api('/clips', { body: new FormData(f) }); f.reset(); await loadSources(); }, 'Clip uploaded');
};

// ---- soundboard + announce ----------------------------------------------------------------

function renderPads() {
  const clips = sources.filter((s) => s.kind === 'clip');
  $('#pads').innerHTML = clips.map((c) => `<button class="pad" data-clip="${c.id}">${esc(c.name)}</button>`).join('') ||
    '<div class="card muted">No clips yet. Upload some under Library.</div>';
}

$('#pads').onclick = (e) => {
  const pad = e.target.closest('[data-clip]');
  if (!pad) return;
  run(() => api('/soundboard', { body: { clip_id: +pad.dataset.clip, speakers: [$('#sb-target').value] } }), `Playing ${pad.textContent}`);
};

$('#tts-go').onclick = () => {
  const text = $('#tts-text').value.trim();
  if (!text) return toast('Type something first', true);
  const btn = $('#tts-go');
  btn.disabled = true;
  run(() => api('/announce', { body: { text, speakers: [$('#tts-target').value], voice: $('#tts-voice').value } }), 'Announced')
    .finally(() => (btn.disabled = false));
};

// ---- recorder -----------------------------------------------------------------------------

const REC_MAX = 60;   // seconds
const rec = { stream: null, recorder: null, chunks: [], blob: null, ext: 'webm', timer: null, started: 0, starting: false };

function recMime() {
  const opts = [['audio/webm;codecs=opus', 'webm'], ['audio/mp4', 'm4a'], ['audio/ogg;codecs=opus', 'ogg']];
  for (const [mime, ext] of opts) if (window.MediaRecorder && MediaRecorder.isTypeSupported(mime)) return { mime, ext };
  return { mime: '', ext: 'webm' };
}

function recTime(sec) {
  const f = (n) => `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
  $('#rec-time').textContent = `${f(sec)} / ${f(REC_MAX)}`;
}

async function recStart() {
  if (rec.starting) return;          // a second tap while the mic permission prompt is open
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return toast('Recording needs a browser with microphone access over HTTPS', true);
  }
  rec.starting = true;
  $('#rec-btn').disabled = true;
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const { mime, ext } = recMime();
    rec.ext = ext;
    rec.chunks = [];
    rec.recorder = new MediaRecorder(rec.stream, mime ? { mimeType: mime } : undefined);
  } catch (e) {
    rec.stream?.getTracks().forEach((t) => t.stop());
    rec.stream = null;
    rec.recorder = null;
    return toast(e.name === 'NotAllowedError' ? 'Microphone access was denied' : `Can't record: ${e.message}`, true);
  } finally {
    rec.starting = false;
    $('#rec-btn').disabled = false;
  }
  rec.recorder.ondataavailable = (e) => e.data.size && rec.chunks.push(e.data);
  rec.recorder.onstop = recFinished;
  rec.recorder.start();
  rec.started = Date.now();
  $('#rec').classList.add('recording');
  $('#rec-label').textContent = 'Stop';
  $('#rec-take').hidden = true;
  rec.timer = setInterval(() => {
    const sec = (Date.now() - rec.started) / 1000;
    recTime(sec);
    if (sec >= REC_MAX) recStop();
  }, 200);
}

function recStop() {
  clearInterval(rec.timer);
  if (rec.recorder && rec.recorder.state !== 'inactive') rec.recorder.stop();
  rec.stream?.getTracks().forEach((t) => t.stop());
  $('#rec').classList.remove('recording');
  $('#rec-label').textContent = 'Record';
}

function recFinished() {
  rec.blob = new Blob(rec.chunks, { type: rec.recorder.mimeType || 'audio/webm' });
  const a = $('#rec-audio');
  if (a.src) URL.revokeObjectURL(a.src);
  a.src = URL.createObjectURL(rec.blob);
  $('#rec-take').hidden = false;
  $('#rec-hint').textContent = 'Listen back, then play it or keep it.';
}

function recDiscard() {
  rec.blob = null;
  const a = $('#rec-audio');
  if (a.src) URL.revokeObjectURL(a.src);
  a.removeAttribute('src');
  $('#rec-take').hidden = true;
  $('#rec-name').value = '';
  $('#rec-hint').textContent = "Records from this device's microphone.";
  recTime(0);
}

$('#rec-btn').onclick = () => (rec.recorder && rec.recorder.state === 'recording' ? recStop() : recStart());
$('#rec-discard').onclick = recDiscard;

$('#rec-play').onclick = () => {
  if (!rec.blob) return;
  const fd = new FormData();
  fd.append('speakers', $('#sb-target').value);
  fd.append('file', rec.blob, `recording.${rec.ext}`);
  run(() => api('/play-once', { body: fd }), 'Playing your recording');
};

$('#rec-save').onclick = () => {
  const name = $('#rec-name').value.trim();
  if (!rec.blob) return;
  if (!name) return toast('Give the clip a name first', true);
  const fd = new FormData();
  fd.append('name', name);
  fd.append('file', rec.blob, `${name}.${rec.ext}`);
  run(async () => { await api('/clips', { body: fd }); recDiscard(); await loadSources(); }, `Saved "${name}"`);
};

// ---- schedules ----------------------------------------------------------------------------

let schedules = [];

async function loadSchedules() {
  if (!sources.length) sources = await api('/sources');
  schedules = await api('/schedules');
  const name = (id) => sources.find((s) => s.id === id)?.name || '(deleted)';
  const spk = (id) => (id === '*' ? 'All speakers' : state.speakers.find((s) => s.id === id)?.name || id);
  $('#schedules').innerHTML = schedules.map((s) => {
    const live = state.speakers.some((p) => p.music && p.music.origin === `schedule:${s.id}`);
    const days = s.days.length === 7 ? 'Every day' : [...s.days].map((d) => DAYS[+d]).join(' ');
    const dates = s.start_date || s.end_date ? `${s.start_date || '…'} → ${s.end_date || '…'}` : '<span class="muted">Any</span>';
    const badge = !s.enabled ? '<span class="badge">Off</span>' : live ? '<span class="badge accent">Playing</span>' : '<span class="badge success">On</span>';
    return `<tr><td>${esc(s.name)}</td><td>${esc(name(s.source_id))}</td><td>${esc(spk(s.speaker_id))}</td><td>${days}</td>
      <td class="mono">${esc(s.start_time)}–${esc(s.end_time)}</td><td class="mono">${dates}</td><td>${badge}</td>
      <td class="actions"><button class="btn ghost sm" data-edit="${s.id}">Edit</button>
      <button class="btn danger sm" data-delsched="${s.id}">Delete</button></td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">No schedules yet.</td></tr>';
}

$('#schedules').onclick = (e) => {
  const ed = e.target.closest('[data-edit]');
  if (ed) openSchedule(schedules.find((s) => s.id === +ed.dataset.edit));
  const del = e.target.closest('[data-delsched]');
  if (del && confirm('Delete this schedule?')) {
    run(async () => { await api(`/schedules/${del.dataset.delsched}`, { method: 'DELETE' }); await loadSchedules(); }, 'Schedule deleted');
  }
};

let editing = null;
function openSchedule(s) {
  editing = s ? s.id : null;
  const f = $('#sched-form');
  $('#sched-title').textContent = s ? 'Edit schedule' : 'New schedule';
  f.source_id.innerHTML = musicOptions();
  speakerOptions(f.speaker_id, true);
  $('#sched-days').innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" value="${i}" ${!s || s.days.includes(i) ? 'checked' : ''}>${d}</label>`).join('');
  f.name.value = s?.name || '';
  if (s) { f.source_id.value = s.source_id; f.speaker_id.value = s.speaker_id; }
  f.start_time.value = s?.start_time || '17:00';
  f.end_time.value = s?.end_time || '21:00';
  f.start_date.value = s?.start_date || '';
  f.end_date.value = s?.end_date || '';
  f.enabled.checked = s ? !!s.enabled : true;
  $('#modal').hidden = false;
  f.name.focus();
}
$('#sched-new').onclick = () => run(async () => { if (!sources.length) sources = await api('/sources'); openSchedule(null); });

$('#sched-form').onsubmit = (e) => {
  e.preventDefault();
  const f = e.target;
  const days = [...$('#sched-days').querySelectorAll('input:checked')].map((i) => i.value).join('');
  if (!days) return toast('Pick at least one day', true);
  const body = {
    name: f.name.value, source_id: +f.source_id.value, speaker_id: f.speaker_id.value, days,
    start_time: f.start_time.value, end_time: f.end_time.value,
    start_date: f.start_date.value || null, end_date: f.end_date.value || null, enabled: f.enabled.checked,
  };
  run(async () => {
    await api(editing ? `/schedules/${editing}` : '/schedules', { method: editing ? 'PUT' : 'POST', body });
    $('#modal').hidden = true;
    await loadSchedules();
  }, 'Schedule saved');
};

// ---- activity -----------------------------------------------------------------------------

async function loadActivity() {
  const rows = await api('/activity');
  $('#activity').innerHTML = rows.map((r) => `<tr><td class="mono">${esc(new Date(r.ts * 1000).toLocaleString())}</td>
    <td>${esc(r.user)}</td><td>${esc(r.action)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">Nothing yet.</td></tr>';
}

// ---- boot ---------------------------------------------------------------------------------

(async () => {
  await Promise.all([poll(), loadSources().catch(() => {})]);
  route();
  setInterval(() => { if (!document.hidden) poll(); }, 3000);
})();
