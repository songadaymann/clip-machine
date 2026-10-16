'use strict';
const $ = id => document.getElementById(id);
const state = {config: null, job: null, selected: null, transcript: null, energy: null, previewExport: true, player: null, playingClip: false, loadingAssets: null};
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const time = seconds => { const whole = Math.floor(seconds); const mins = Math.floor(whole / 60); const secs = whole % 60; return `${mins}:${String(secs).padStart(2, '0')}`; };
const preciseTime = seconds => `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(2).padStart(5, '0')}`;
const parseTime = value => { const parts = value.trim().split(':'); if (parts.length > 3 || parts.some(p => !/^\d+(\.\d+)?$/.test(p))) throw new Error('Use minutes:seconds, or seconds.'); return parts.reduce((n, p) => n * 60 + Number(p), 0); };
const clip = () => state.job?.clips.find(c => c.id === state.selected);
const fileUrl = name => `/api/jobs/${state.job.id}/files/${encodeURIComponent(name.split('/').pop())}`;
const busy = () => state.job?.busy;
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Clip-Token': state.config.token}, body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
async function upload(file) {
  const response = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {method: 'POST', headers: {'X-Clip-Token': state.config.token}, body: file});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Upload failed');
  return result.id;
}
async function history() {
  const jobs = await api('/api/jobs');
  $('history').hidden = !jobs.length;
  $('history-list').innerHTML = jobs.slice(0, 12).map(j => `<button class="history-entry" data-job="${j.id}"><span>${escape(j.source_name)}</span><span>${escape(j.status)} ↗</span></button>`).join('');
}
async function openJob(id) {
  clearError();
  state.player?.pause();
  state.job = await api(`/api/jobs/${id}`);
  state.selected = state.job.clips[0]?.id;
  state.transcript = null; state.energy = null; state.loadingAssets = null;
  state.previewExport = true; state.playingClip = false;
  $('ingest').hidden = true; $('workbench').hidden = false;
  window.scrollTo(0, 0);
  location.hash = id;
  renderJob(true);
  await loadAssets();
}
async function loadAssets() {
  if (!state.job?.media || state.transcript || state.loadingAssets === state.job.id) return;
  const identity = state.job.id;
  state.loadingAssets = identity;
  try {
    const [transcript, energy] = await Promise.all([api(`/api/jobs/${identity}/transcript`), api(`/api/jobs/${identity}/energy`)]);
    if (state.job?.id !== identity) return;
    state.transcript = transcript; state.energy = energy;
    renderTranscript(); drawWaveform();
  } catch (_) { /* Assets are not available during transcription; polling retries. */ }
  finally { if (state.loadingAssets === identity) state.loadingAssets = null; }
}
function renderJob(force = false) {
  const job = state.job;
  $('episode-name').textContent = job.source_name;
  $('episode-status').textContent = job.message;
  $('demo-note').hidden = job.options.provider !== 'demo-fixture';
  const processing = busy() && !job.clips.length;
  $('processing').hidden = !processing;
  $('processing-message').textContent = job.message;
  $('review').hidden = !job.clips.length;
  $('export-all').disabled = busy() || !job.clips.some(c => c.decision !== 'rejected');
  $('retry').hidden = job.status !== 'failed' || Boolean(job.clips.length);
  $('retry').disabled = busy();
  if (job.error && !busy()) showError(new Error(job.error)); else clearError();
  if (!job.clips.length) return;
  if (!clip()) { state.selected = job.clips[0].id; force = true; }
  $('clip-count').textContent = String(job.clips.length);
  $('episode-duration').textContent = time(job.media.duration);
  $('clips').innerHTML = job.clips.map((c, i) => `<button class="clip-item ${c.id === state.selected ? 'active' : ''} ${c.decision === 'rejected' ? 'rejected' : ''}" data-clip="${c.id}" aria-pressed="${c.id === state.selected}"><span class="clip-number">${String(i + 1).padStart(2, '0')}</span><span><strong>${escape(c.title)}</strong><small>${time(c.start)}–${time(c.end)} · ${Math.round(c.end - c.start)}s${c.decision === 'approved' ? ' · Kept' : c.decision === 'rejected' ? ' · Rejected' : ''}</small></span><span class="clip-score">${c.score}</span></button>`).join('');
  renderClip(force);
}
function renderClip(force = false) {
  const c = clip(); if (!c) return;
  $('clip-title').textContent = c.title;
  $('clip-time').textContent = `${time(c.start)}–${time(c.end)} · ${(c.end-c.start).toFixed(1)}s`;
  $('clip-reason').textContent = c.reason;
  $('scores').innerHTML = ['hook', 'flow', 'value', 'trend'].map(key => `<div class="score"><span>${key[0].toUpperCase()+key.slice(1)}</span><strong>${c.scores[key] ?? '—'}</strong></div>`).join('');
  $('scores').title = 'Editorial ranking, not viral probability. 35% hook + 35% flow + 30% value. Trend is not evaluated.';
  $('context-note').textContent = c.context_note + ' Trend is not evaluated.';
  $('edited-note').hidden = c.start === c.original_start && c.end === c.original_end;
  if (force || !['trim-start', 'trim-end'].includes(document.activeElement?.id)) {
    $('trim-start').value = preciseTime(c.start); $('trim-end').value = preciseTime(c.end);
  }
  for (const id of ['approve', 'reject', 'export-clip', 'trim-start', 'trim-end']) $(id).disabled = busy();
  $('trim-form').querySelector('[type=submit]').disabled = busy();
  $('approve').textContent = c.decision === 'approved' ? 'Kept ✓' : 'Keep clip';
  $('reject').textContent = c.decision === 'rejected' ? 'Restore clip' : 'Reject';
  $('export-clip').disabled = busy() || c.decision === 'rejected';
  $('downloads').innerHTML = c.export ? `<a download href="${fileUrl(c.export.media)}">Download ${c.export.media.endsWith('.mp4') ? 'video' : 'audio'}</a><a download href="${fileUrl(c.export.srt)}">SRT</a><a download href="${fileUrl(c.export.vtt)}">VTT</a>` : '';
  const useExport = c.export && state.previewExport;
  const isVideo = useExport ? c.export.media.endsWith('.mp4') : state.job.media.has_video;
  state.player = isVideo ? $('player') : $('audio-player');
  $('player').hidden = !isVideo; $('audio-player').hidden = isVideo;
  const url = useExport ? fileUrl(c.export.media) : `/api/jobs/${state.job.id}/source`;
  const changed = state.player.getAttribute('src') !== url;
  $('preview-label').textContent = useExport ? 'Export preview' : 'Source preview';
  $('preview-toggle').hidden = !c.export;
  $('preview-toggle').textContent = useExport ? 'View source' : 'View export';
  if (changed) {
    $('player').pause(); $('audio-player').pause();
    state.player.src = url;
    state.player.replaceChildren();
    if (useExport) {
      const track = document.createElement('track');
      track.kind = 'captions'; track.label = 'Captions'; track.srclang = state.transcript?.language || 'en'; track.src = fileUrl(c.export.vtt);
      // Burned captions are already in the rendered pixels. Keep the optional
      // accessible track off by default to avoid doubled captions.
      track.default = !c.export.burn_captions;
      state.player.append(track);
    }
  }
  if ((changed || force) && !useExport) seek(c.start);
  if (force) { renderTranscript(); drawWaveform(); }
}
function seek(seconds, play = false) {
  const player = state.player;
  const apply = () => { player.currentTime = seconds; if (play) player.play().catch(showError); };
  if (player.readyState >= 1) apply(); else player.addEventListener('loadedmetadata', apply, {once: true});
}
function sourceAt(seconds, play = false) {
  state.playingClip = false; state.previewExport = false; renderClip(); seek(seconds, play);
}
function renderTranscript() {
  if (!state.transcript || !clip()) return;
  const c = clip();
  $('transcript').innerHTML = state.transcript.segments.map(s => `<div class="transcript-line ${s.end > c.start && s.start < c.end ? 'in-clip' : ''}"><button data-seek="${s.start}" aria-label="Play from ${time(s.start)}">${time(s.start)}</button><div><p>${escape(s.text)}</p><div class="boundary-buttons"><button data-boundary="start" data-value="${s.start}" ${busy() ? 'disabled' : ''}>Start here</button><button data-boundary="end" data-value="${s.end}" ${busy() ? 'disabled' : ''}>End here</button></div></div></div>`).join('');
}
function drawWaveform() {
  if (!state.energy || !clip()) return;
  const windows = state.energy.windows, duration = state.job.media.duration, c = clip();
  let svg = `<rect class="selection" x="${900*c.start/duration}" y="0" width="${900*(c.end-c.start)/duration}" height="64"/>`;
  for (let i = 0; i < 180; i++) {
    const a = Math.floor(i*windows.length/180), b = Math.max(a+1, Math.floor((i+1)*windows.length/180));
    const db = Math.max(...windows.slice(a,b).map(w => w.dbfs), -60);
    const height = Math.max(2, (Math.max(-60, db)+60)/60*54);
    svg += `<rect class="energy" x="${i*5}" y="${(64-height)/2}" width="2" height="${height}"/>`;
  }
  svg += `<line class="boundary" x1="${900*c.start/duration}" x2="${900*c.start/duration}" y1="0" y2="64"/><line class="boundary" x1="${900*c.end/duration}" x2="${900*c.end/duration}" y1="0" y2="64"/>`;
  $('waveform').innerHTML = svg;
}
async function mutate(action, body) {
  clearError();
  state.job = await api(`/api/jobs/${state.job.id}/${action}`, body);
  renderJob(action === 'edit');
}
async function safely(action) { try { await action(); } catch (error) { showError(error); } }
$('episode-form').addEventListener('submit', event => { event.preventDefault(); safely(async () => {
  clearError();
  const file = $('source-file').files[0], transcript = $('transcript-file').files[0];
  if (!file) throw new Error('Choose an episode first.');
  if (!transcript && !state.config.capabilities.mlx) { $('episode-form').querySelector('details').open = true; throw new Error('Local transcription is not installed. Add a timestamped transcript JSON, or follow the setup guide in the repository.'); }
  if (Number($('minimum').value) > Number($('maximum').value)) throw new Error('Minimum duration must be below maximum duration.');
  $('analyze').disabled = true; $('analyze').textContent = 'Importing episode…';
  try {
    const source = await upload(file), transcriptId = transcript ? await upload(transcript) : null;
    const job = await api('/api/jobs', {source, source_name: file.name, transcript: transcriptId, options: {provider: $('provider').value, direction: $('direction').value,
      count: Number($('count').value), min_seconds: Number($('minimum').value), max_seconds: Number($('maximum').value), layout: $('layout').value, auto_render: true, burn_captions: true}});
    await openJob(job.id);
  } finally { $('analyze').disabled = false; $('analyze').textContent = 'Find clips ↗'; }
}); });
$('source-file').addEventListener('change', () => { $('file-label').textContent = $('source-file').files[0]?.name || 'Choose an episode'; });
for (const name of ['dragover', 'dragleave', 'drop']) $('drop-zone').addEventListener(name, event => {
  event.preventDefault(); $('drop-zone').classList.toggle('dragging', name === 'dragover');
  if (name === 'drop' && event.dataTransfer.files.length) { $('source-file').files = event.dataTransfer.files; $('source-file').dispatchEvent(new Event('change')); }
});
$('demo').addEventListener('click', () => safely(async () => { clearError(); $('demo').disabled = true; $('demo').textContent = 'Preparing sample…'; try { await openJob((await api('/api/demo', {})).id); } finally { $('demo').disabled = false; $('demo').textContent = 'Try a sample episode'; } }));
$('new-episode').addEventListener('click', () => { state.player?.pause(); state.job = null; location.hash = ''; $('ingest').hidden = false; $('workbench').hidden = true; clearError(); safely(history); });
$('history-list').addEventListener('click', event => { const button = event.target.closest('[data-job]'); if (button) safely(() => openJob(button.dataset.job)); });
$('clips').addEventListener('click', event => { const button = event.target.closest('[data-clip]'); if (!button) return; state.player?.pause(); state.selected = button.dataset.clip; state.playingClip = false; state.previewExport = true; renderJob(true); });
$('preview-toggle').addEventListener('click', () => { state.previewExport = !state.previewExport; state.playingClip = false; renderClip(true); });
$('play-clip').addEventListener('click', () => { if (!clip()) return; state.playingClip = true; seek(state.previewExport && clip().export ? 0 : clip().start, true); });
for (const player of [$('player'), $('audio-player')]) player.addEventListener('timeupdate', () => { const c = clip(); if (c && state.playingClip && !(state.previewExport && c.export) && player.currentTime >= c.end) { player.pause(); state.playingClip = false; } });
$('trim-form').addEventListener('submit', event => { event.preventDefault(); safely(() => mutate('edit', {clip_id: state.selected, start: parseTime($('trim-start').value), end: parseTime($('trim-end').value)})); });
$('approve').addEventListener('click', () => safely(() => mutate('edit', {clip_id: state.selected, decision: 'approved'})));
$('reject').addEventListener('click', () => safely(() => mutate('edit', {clip_id: state.selected, decision: clip().decision === 'rejected' ? 'unreviewed' : 'rejected'})));
$('export-clip').addEventListener('click', () => safely(() => mutate('export', {clip_id: state.selected})));
$('export-all').addEventListener('click', () => safely(() => mutate('export', {})));
$('retry').addEventListener('click', () => safely(() => mutate('retry', {})));
$('transcript').addEventListener('click', event => { const target = event.target.closest('button'); if (!target) return; if (target.dataset.seek) sourceAt(Number(target.dataset.seek), true); if (target.dataset.boundary) { $(target.dataset.boundary === 'start' ? 'trim-start' : 'trim-end').value = preciseTime(Number(target.dataset.value)); $('trim-form').scrollIntoView({behavior: 'smooth', block: 'center'}); } });
$('waveform').addEventListener('click', event => { const box = $('waveform').getBoundingClientRect(); sourceAt((event.clientX-box.left)/box.width*state.job.media.duration); });
$('waveform').addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); sourceAt(clip().start); } });
async function poll() {
  try {
    if (state.job) {
      const id = state.job.id, previous = state.job.updated_at, wasBusy = state.job.busy;
      const job = await api(`/api/jobs/${id}`);
      if (state.job?.id !== id) return;
      state.job = job;
      if (previous !== job.updated_at || wasBusy !== job.busy) renderJob(!state.transcript);
      await loadAssets();
    }
  } catch (error) { if (state.job) showError(new Error('Local engine disconnected. Keep the server running, then reload.')); }
  finally { setTimeout(poll, 1500); }
}
safely(async () => {
  state.config = await api('/api/config');
  $('transcription-note').textContent = state.config.capabilities.mlx ? 'Local transcription available.' : 'Have a transcript? Add it under Duration & transcript. Local transcription needs setup.';
  if (!state.config.capabilities.ffmpeg || !state.config.capabilities.ffprobe) showError(new Error('FFmpeg and ffprobe are required. See the repository setup guide.'));
  if (/^#[a-f0-9]{32}$/.test(location.hash)) await openJob(location.hash.slice(1));
  await history();
  poll();
});
