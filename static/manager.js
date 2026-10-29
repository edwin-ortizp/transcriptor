'use strict';
window.name = 'transcriptor-sessions';
const $ = id => document.getElementById(id);
const stateNames = {none:'Sin análisis', pending:'Pendiente', running:'Analizando', completed:'Completado', error:'Error', interrupted:'Interrumpido'};
const captureNames = {ready:'Listo', starting:'Iniciando', recording:'Grabando', pausing:'Pausando', paused:'En pausa', finishing:'Terminando', finished:'Finalizada'};
let rows = [], selected = null, detail = null, native = false, privateView = true;
let captureRevision = -1;
let captureState = 'ready', captureId = null, outputEnabled = true, refreshTimer, noticeTimer;
let parts = [], partIndex = -1, duration = 0;
const player = $('player');
async function api(url, method = 'GET', body) {
  const r = await fetch(url, {method, headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined});
  const data = await r.json();
  if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Revisa los campos y sus límites');
  return data;
}
function notify(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true, 7000); }
function error(e) { notify(e.message); }
function stamp(value) { const n = Math.max(0, Math.floor(value || 0)); return [Math.floor(n/3600), Math.floor(n/60)%60,n%60].map(x => String(x).padStart(2,'0')).join(':'); }
function dateText(value) { if (!value) return ''; const date = new Date(value); return isNaN(date) ? '' : date.toLocaleString('es-CO', {dateStyle:'medium',timeStyle:'short'}); }
const playbackBlocked = () => privateView || (outputEnabled && !['ready','finished'].includes(captureState));

function renderList() {
  $('sessionList').replaceChildren();
  const query = $('search').value.toLocaleLowerCase();
  const visible = rows.filter(r => (r.title || '').toLocaleLowerCase().includes(query));
  if (!visible.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'No hay sesiones para mostrar.'; $('sessionList').append(p); }
  visible.forEach(row => {
    const button = document.createElement('button'); button.className = 'session-row' + (row.session_id === selected ? ' selected' : '');
    button.dataset.sessionId = row.session_id;
    const title = document.createElement('strong'); title.textContent = row.title;
    const meta = document.createElement('small'); meta.textContent = `${dateText(row.date)} · ${stamp(row.duration)}`;
    const status = document.createElement('small'); status.className = 'status-dot';
    status.textContent = row.state && row.state !== 'finished' ? captureNames[row.state] : `${row.legacy ? 'TXT antiguo · ' : ''}${stateNames[row.analysis_state] || 'Sin análisis'}`;
    button.append(title,meta,status); button.onclick = () => loadDetail(row.session_id, true).catch(error); $('sessionList').append(button);
  });
}
async function refreshList() {
  const data = await api('/sessions'); rows = data.sessions; renderList();
  $('listWarnings').textContent = data.warnings.join('\n');
  if (!selected && rows.length) await loadDetail(rows[0].session_id, true);
}
function forgetSession(sid) {
  rows = rows.filter(r => r.session_id !== sid);
  if (selected === sid) {
    selected = null; detail = null; clearPlayer();
    $('sessionDetail').hidden = true; $('empty').hidden = false;
  }
  renderList();
  refreshList().catch(error);
}
async function loadDetail(sid, changing = false) {
  if (changing || sid !== selected) {
    selected = sid; clearPlayer(); renderList(); window.resetSuspects?.();
    analysisJob = null; clearInterval(analysisWaitTimer);
    $('analyze').disabled = true; $('continueAnalysis').hidden = true;
  }
  const data = await api('/sessions/' + encodeURIComponent(sid));
  if (selected !== sid) return;
  detail = data; $('empty').hidden = true; $('sessionDetail').hidden = false;
  $('sessionTitle').textContent = data.title;
  $('sessionMeta').textContent = `${dateText(data.date)} · ${stamp(data.duration)}${data.legacy ? ' · TXT antiguo, sin audio ni contexto' : ''}`;
  $('sessionBadge').textContent = captureNames[data.state] || 'Guardada';
  $('sessionWarning').textContent = data.failure_text || data.archive_error || ''; $('sessionWarning').hidden = !$('sessionWarning').textContent;
  $('sessionContext').textContent = data.context || ''; $('sessionContextBox').hidden = !data.context;
  if (changing) $('analysisTemplate').value = data.template || 'meeting';
  renderTranscript(data); renderMarkers(data.markers || []);
  if (changing || (data.state === 'finished' && !parts.length)) setupPlayer(data);
  updatePlayback();
  document.dispatchEvent(new CustomEvent('session-detail-loaded',{detail:data}));
  await loadAnalysis(sid);
  window.loadSuspects?.();
}
function renderTranscript(data) {
  const scroll = $('tab-transcript').scrollTop;
  $('transcript').replaceChildren();
  data.history.forEach(item => {
    const block = document.createElement('div'); block.className = 'fragment' + (item.type === 'note' ? ' note' : '');
    block.dataset.eventId = item.id; block.dataset.start = item.start; block.dataset.end = item.end ?? item.start + 1;
    block.tabIndex = 0; block.onclick = () => seek(item.start);
    block.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); seek(item.start); } };
    const meta = document.createElement('div'); meta.className = 'fragment-meta';
    const source = document.createElement('span'); source.className = 'source' + (item.source === 'Yo' ? ' yo' : ''); source.textContent = item.source || 'Nota';
    const time = document.createElement('span'); time.textContent = item.time; meta.append(source,time);
    const text = document.createElement('div'); text.textContent = item.text; block.append(meta,text); $('transcript').append(block);
  });
  [...$('transcript').children].sort((a,b) => +a.dataset.start - +b.dataset.start).forEach(el => $('transcript').append(el));
  $('tab-transcript').scrollTop = scroll;
}
function renderMarkers(markers) {
  $('markers').replaceChildren(); $('markerCount').textContent = markers.length ? `(${markers.length})` : '';
  if (!markers.length) $('markers').textContent = 'Esta sesión no tiene marcadores.';
  markers.forEach(marker => {
    const row = document.createElement('div'); row.className = 'marker-row';
    const jump = document.createElement('button'); jump.className = 'marker-jump'; jump.textContent = `${stamp(marker.start)} · ${marker.text}`; jump.onclick = () => { showTab('transcript'); seek(marker.start); };
    const edit = document.createElement('button'); edit.textContent = 'Renombrar'; edit.onclick = async () => {
      const text = prompt('Nombre del marcador (máximo 300 caracteres)', marker.text);
      if (text === null) return;
      try { await api(`/sessions/${selected}/markers/${marker.id}`, 'PATCH', {text}); await loadDetail(selected); } catch (e) { error(e); }
    };
    const remove = document.createElement('button'); remove.textContent = 'Eliminar'; remove.onclick = async () => {
      try { await api(`/sessions/${selected}/markers/${marker.id}`, 'DELETE'); await loadDetail(selected); } catch (e) { error(e); }
    };
    row.append(jump,edit,remove); $('markers').append(row);
  });
}
function showTab(name) {
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.hidden = p.id !== 'tab-' + name);
  if (name !== 'transcript') player.pause();
}
document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => showTab(button.dataset.tab));
function clearPlayer() { player.pause(); player.removeAttribute('src'); player.load(); parts = []; partIndex = -1; duration = 0; $('playback').hidden = true; }
function setupPlayer(data) {
  clearPlayer(); parts = data.parts || []; duration = data.duration || 0; $('seek').max = duration; $('seek').value = 0;
  const first = parts.find(p => p.available); $('playback').hidden = !first;
  if (first && !playbackBlocked()) seek(first.start);
}
function updatePlayback() {
  const missing = parts.filter(p => !p.available).length;
  if (playbackBlocked()) { player.pause(); $('audioStatus').textContent = 'Reproducción bloqueada mientras se captura el audio del equipo'; }
  else $('audioStatus').textContent = detail?.backup_error ? `Respaldo incompleto: ${detail.backup_error}` : !detail?.backup_enabled ? 'Sin respaldo de audio' : missing ? `Faltan ${missing} archivos de audio` : 'Audio local · sincronización por bloque';
  if (detail?.state !== 'finished') { player.pause(); $('audioStatus').textContent = 'El audio estará disponible al terminar la sesión'; }
}
function seek(seconds, play = false) {
  if (playbackBlocked() || detail?.state !== 'finished') { updatePlayback(); return; }
  const index = parts.findIndex(p => seconds >= p.start && seconds < p.start + p.duration);
  if (index < 0 || !parts[index].available) {
    const lines = [...$('transcript').children].sort((a,b) => Math.abs(+a.dataset.start-seconds)-Math.abs(+b.dataset.start-seconds));
    lines[0]?.scrollIntoView({block:'center'});
    if (parts.length) notify('No hay audio disponible en ese momento');
    return;
  }
  const resume = play || !player.paused;
  if (partIndex !== index) { partIndex = index; player.src = parts[index].url; }
  player.currentTime = Math.max(0, seconds - parts[index].start); player.playbackRate = +$('rate').value;
  if (resume) player.play().catch(error);
}
player.onplay = () => { if (playbackBlocked() || detail?.state !== 'finished') player.pause(); else if (partIndex < 0 && parts.length) seek(parts.find(p => p.available).start, true); };
player.ontimeupdate = () => {
  if (partIndex < 0) return;
  const time = parts[partIndex].start + player.currentTime; $('seek').value = time; $('playTime').textContent = `${stamp(time)} / ${stamp(duration)}`;
  document.querySelectorAll('.fragment').forEach(el => el.classList.toggle('playing', time >= +el.dataset.start && time < +el.dataset.end));
};
player.onended = () => { if (parts[partIndex + 1]) seek(parts[partIndex + 1].start, true); };
player.onerror = () => { if (player.getAttribute('src')) notify('No se pudo reproducir el archivo de audio'); };
$('seek').oninput = () => seek(+$('seek').value);
$('rate').onchange = () => { player.playbackRate = +$('rate').value; };
$('copyText').onclick = () => { if (!privateView && detail) navigator.clipboard.writeText(detail.history.map(x => `[${x.time}] ${x.source || 'Nota'}: ${x.text}`).join('\n')).catch(error); };

let analysisJob = null, analysisWaitTimer;
const stageNames = {pending:'Preparando', extract:'Extracción de secciones', consolidate:'Consolidando notas', compact:'Compactando notas', final:'Síntesis final', completed:'Completado'};
async function loadAnalysis(sid) {
  const data = await api(`/sessions/${sid}/analysis`); if (sid !== selected) return;
  analysisJob = data; clearInterval(analysisWaitTimer);
  const busy = ['pending','running'].includes(data.state);
  $('analyze').disabled = busy || detail.state !== 'finished';
  $('analyze').textContent = busy ? stateNames[data.state] : data.result || data.can_continue ? 'Regenerar' : ['error','interrupted'].includes(data.state) ? 'Reintentar' : 'Analizar';
  $('continueAnalysis').hidden = !data.can_continue;
  $('continueAnalysis').disabled = busy;
  $('analysisTemplate').disabled = busy;
  $('analysisState').classList.toggle('error', ['error','interrupted'].includes(data.state));
  const updateState = () => {
    const p = data.progress;
    const waiting = busy && data.wait;
    $('analysisState').textContent = `${waiting ? 'Esperando cuota' : stateNames[data.state] || 'Sin análisis'}${p ? ` · ${stageNames[p.stage] || ''} · ${p.completed_sections || 0}/${p.total_sections || 0} secciones · ${p.completed_calls} llamadas realizadas · ${p.total_calls} previstas (mínimo) · límite ${p.max_calls ?? '—'}` : ''}${waiting ? `\n${data.wait.reason} · aproximadamente ${Math.max(0,Math.ceil(data.wait.until-Date.now()/1000))} s` : ''}${data.error ? '\n' + data.error : ''}${data.stale ? '\nTranscripción modificada: análisis desactualizado. Regenera para analizar el texto actual.' : ''}`;
  };
  updateState(); if (busy && data.wait) analysisWaitTimer = setInterval(updateState,1000);
  const result = data.result; $('analysisResult').replaceChildren(); $('analysisMeta').textContent = '';
  if (result) {
    $('analysisMeta').textContent = `${data.state !== 'completed' ? 'Último resultado válido · ' : ''}${dateText(result.created_at)} · ${result.model} · prompt v${result.prompt_version} · ${result.calls} llamadas${result.usage?.total_tokens !== undefined ? ' · ' + result.usage.total_tokens + ' tokens reportados' : ''}${result.usage_complete === false ? ' · Consumo incompleto' : ''}${result.estimated_cost_usd !== undefined ? ' · estimado USD ' + result.estimated_cost_usd.toFixed(4) : ''}${result.token_counter ? ' · ' + result.token_counter : ''}`;
    renderAnalysis(result.text);
  }
  const opened = new Set([...$('analysisDetails').querySelectorAll('details[open]')].map(el => el.dataset.stageId));
  $('analysisDetails').replaceChildren();
  const partials = (data.stages || []).filter(n => n.stage === 'extract' && n.state === 'completed');
  const sections = partials.length ? partials : result?.details || [];
  if (sections.length) {
    const hint = document.createElement('p'); hint.className = 'hint';
    hint.textContent = data.state === 'completed' ? 'Notas por sección; consulta el texto original para todos los detalles.' : partials.length ? 'Avances parciales del trabajo actual. La síntesis anterior, si existe, se conserva por separado.' : 'Detalle del último resultado válido; el nuevo trabajo todavía no tiene secciones completadas.';
    $('analysisDetails').append(hint);
    sections.forEach((section,index) => {
      const box = document.createElement('details'); box.className = 'analysis-section'; box.dataset.stageId = section.id; box.open = opened.has(section.id);
      const title = document.createElement('summary'); title.textContent = `Sección ${index+1} · ${stamp(section.start)} – ${stamp(section.end)}`;
      const jump = document.createElement('button'); jump.textContent = 'Ir al texto / audio'; jump.onclick = () => navigateAnalysis(section.start);
      const body = document.createElement('div'); renderAnalysis(section.text,body); box.append(title,jump,body); $('analysisDetails').append(box);
    });
  } else { $('analysisDetails').textContent = result ? 'Este análisis anterior no conserva detalle por secciones.' : 'Las secciones aparecerán al completar cada extracción.'; }
}
function navigateAnalysis(seconds) {
  if (privateView) return;
  showTab('transcript');
  const lines = [...$('transcript').children].sort((a,b) => Math.abs(+a.dataset.start-seconds)-Math.abs(+b.dataset.start-seconds));
  lines[0]?.scrollIntoView({block:'center'});
  document.querySelectorAll('.fragment.playing').forEach(el => el.classList.remove('playing'));
  lines[0]?.classList.add('playing'); seek(seconds);
}
$('showSynthesis').onclick = () => { $('analysisResult').hidden = false; $('analysisDetails').hidden = true; $('showSynthesis').setAttribute('aria-pressed','true'); $('showAnalysisDetails').setAttribute('aria-pressed','false'); };
$('showAnalysisDetails').onclick = () => { $('analysisResult').hidden = true; $('analysisDetails').hidden = false; $('showSynthesis').setAttribute('aria-pressed','false'); $('showAnalysisDetails').setAttribute('aria-pressed','true'); };
$('continueAnalysis').onclick = async () => {
  if (!analysisJob || privateView || analysisJob.session_id !== selected) return;
  const sid = selected, job = analysisJob;
  if (job.uncertain && !confirm('Una solicitud no tiene resultado confirmado y podría haber consumido cuota. ¿Continuar y autorizar repetirla si hace falta?')) return;
  $('continueAnalysis').disabled = true;
  try { await api(`/analysis-jobs/${job.id}/continue`, 'POST', {acknowledge_uncertain:!!job.uncertain}); await loadAnalysis(sid); refreshList().catch(error); }
  catch (e) { error(e); if (selected === sid) await loadAnalysis(sid); }
};
function renderAnalysis(text, target = $('analysisResult')) {
  text.split('\n').forEach(line => {
    const heading = /^(#{1,4})\s+/.exec(line);
    const block = document.createElement(heading ? 'h' + Math.min(4, heading[1].length + 1) : 'div');
    const value = heading ? line.slice(heading[0].length) : line;
    for (const token of value.split(/(\*\*[^*]+\*\*|\[\d+:\d{2}:\d{2}\])/)) {
      if (/^\[\d+:\d{2}:\d{2}\]$/.test(token)) {
        const ref = document.createElement('button'); ref.className = 'reference'; ref.textContent = token;
        ref.onclick = () => { const [h,m,s] = token.slice(1,-1).split(':').map(Number); navigateAnalysis(h*3600+m*60+s); }; block.append(ref);
      } else if (token.startsWith('**') && token.endsWith('**')) { const strong = document.createElement('strong'); strong.textContent = token.slice(2,-2); block.append(strong); }
      else block.append(document.createTextNode(token));
    }
    if (!value) block.append(document.createElement('br'));
    target.append(block);
  });
}
$('analyze').onclick = async () => {
  if (privateView || detail?.session_id !== selected) return;
  try { await api(`/sessions/${selected}/analysis`, 'POST', {template:$('analysisTemplate').value}); await loadAnalysis(selected); refreshList().catch(error); }
  catch (e) { error(e); }
};
$('refresh').onclick = () => refreshList().catch(error);
$('search').oninput = renderList;
$('openSession').onclick = async () => {
  try {
    const path = native ? '' : prompt('Ruta completa a session.json o un TXT'); if (path === null) return;
    const data = await api('/open-session', 'POST', {path}); await refreshList(); await loadDetail(data.session_id, true);
  } catch (e) { error(e); }
};
$('showWidget').onclick = () => { if (native) api('/show-widget','POST').catch(error); else { const target = window.open('', 'transcriptor-widget'); if (target) { try { if (target.location.href === 'about:blank') target.location.href = '/'; target.focus(); } catch (_) { window.open('/', 'transcriptor-widget'); } } } };
function setPrivate(enabled) {
  privateView = enabled; document.dispatchEvent(new Event('privacy-changed')); document.body.classList.toggle('private', enabled); $('privacyCover').hidden = !enabled;
  $('privacy').textContent = enabled ? 'Mostrar contenido' : 'Ocultar contenido'; $('privacy').setAttribute('aria-pressed', String(enabled));
  if (enabled) { player.pause(); document.querySelectorAll('dialog[open]').forEach(d => d.close()); document.getSelection()?.removeAllRanges(); document.activeElement?.blur(); }
}
$('privacy').onclick = () => { setPrivate(!privateView); api('/discretion','POST',{enabled:privateView}).catch(error); };
document.addEventListener('copy', e => { if (privateView) e.preventDefault(); });

// Preparation belongs to the manager; the widget can start with this saved draft.
$('prepare').onclick = async () => {
  try {
    const [draft, sources, devices] = await Promise.all([api('/preparation'),api('/sources'),api('/devices')]);
    $('draftTitle').value = draft.title; $('draftContext').value = draft.context; $('draftTemplate').value = draft.template; $('draftBackup').checked = draft.backup_audio;
    for (const [id, list, source] of [['micDevice',devices.mics,sources.Yo],['outputDevice',devices.speakers,sources.Otros]]) {
      $(id).replaceChildren(new Option('Predeterminado','')); list.forEach(d => $(id).add(new Option(d.name,d.id))); $(id).value = source.id;
    }
    $('useMic').checked = sources.Yo.enabled; $('useOutput').checked = sources.Otros.enabled;
    $('preparationError').textContent = ''; $('probeResults').replaceChildren(); validateContext(); $('preparationDialog').showModal();
  } catch (e) { error(e); }
};
function validateContext() {
  const length = Array.from($('draftContext').value).length;
  $('contextHint').textContent = `${length}/2000 caracteres${length > 2000 ? ' · Reduce el contexto para guardar' : ''}`;
  $('contextHint').classList.toggle('error', length > 2000); $('draftContext').setAttribute('aria-invalid', String(length > 2000));
  $('savePreparation').disabled = length > 2000 || !['ready','finished'].includes(captureState);
  return length <= 2000;
}
$('draftContext').oninput = validateContext;
$('preparationForm').onsubmit = async event => {
  event.preventDefault(); if (!validateContext()) return;
  try { await api('/preparation','POST',{title:$('draftTitle').value, context:$('draftContext').value, template:$('draftTemplate').value, backup_audio:$('draftBackup').checked}); $('preparationDialog').close(); notify('Preparación guardada. Inicia desde el widget.'); }
  catch (e) { $('preparationError').textContent = e.message; }
};
async function sourceChange(source, enabled, id) {
  try { await api('/probe/stop','POST'); await api('/source','POST',{source, enabled, device_id:id}); $('probeResults').textContent = 'Fuente actualizada. Repite la prueba.'; }
  catch (e) { $('preparationError').textContent = e.message; }
}
$('useMic').onchange = $('micDevice').onchange = () => sourceChange('Yo',$('useMic').checked,$('micDevice').value);
$('useOutput').onchange = $('outputDevice').onchange = () => sourceChange('Otros',$('useOutput').checked,$('outputDevice').value);
$('probeStart').onclick = () => api('/probe/start','POST').then(showProbe).catch(e => $('preparationError').textContent = e.message);
$('probeStop').onclick = () => api('/probe/stop','POST').then(() => $('probeResults').textContent = 'Prueba detenida').catch(error);
function showProbe(message) {
  if (!$('preparationDialog').open) return;
  $('probeResults').replaceChildren();
  for (const [source, result] of Object.entries(message.results)) {
    const row = document.createElement('div'); row.className = 'hint';
    const names = {opening:'Abriendo…', disabled:'Apagado', signal:'Señal detectada', silence:'Abierto · silencio (no es un error)', error:'Error'};
    row.textContent = `${source} · ${result.name || ''} · ${names[result.state]}${result.error ? ': ' + result.error : ''}${result.fallback ? ' · Predeterminado' : ''}`;
    const meter = document.createElement('meter'); meter.max = 1; meter.value = result.level || 0; meter.setAttribute('aria-label','Nivel de ' + source); row.append(meter); $('probeResults').append(row);
  }
}
$('preparationDialog').addEventListener('close', () => api('/probe/stop','POST').catch(error));
document.querySelectorAll('.close-dialog').forEach(button => button.onclick = () => button.closest('dialog').close());

const configFields = {baseUrl:'base_url',apiModel:'model',outputParam:'output_token_field',chunkChars:'chunk_chars',outputTokens:'max_output_tokens',contextTokens:'context_window_tokens',partialTokens:'partial_output_tokens',consolidationTokens:'consolidation_output_tokens',finalTokens:'final_output_tokens',requestsMinute:'requests_per_minute',tokensMinute:'tokens_per_minute',tokenizerEncoding:'tokenizer_encoding',maxCalls:'max_calls',timeout:'timeout_seconds',inputPrice:'input_price',outputPrice:'output_price'};
$('configure').onclick = async () => {
  try { const config = await api('/analysis-settings'); Object.entries(configFields).forEach(([id,key]) => $(id).value = config[key] ?? ''); $('automatic').checked = config.automatic; $('apiKey').value = ''; $('clearKey').checked = false; $('keyState').textContent = config.has_key ? 'Clave guardada en las credenciales de Windows' : 'Sin clave para esta URL'; $('settingsState').textContent = ''; $('settingsDialog').showModal(); }
  catch (e) { error(e); }
};
$('baseUrl').oninput = () => { $('keyState').textContent = 'La clave se asocia a la URL exacta que guardes.'; };
$('settingsForm').onsubmit = async event => {
  event.preventDefault(); const config = {automatic:$('automatic').checked,clear_key:$('clearKey').checked};
  Object.entries(configFields).forEach(([id,key]) => config[key] = ['baseUrl','apiModel','outputParam','tokenizerEncoding'].includes(id) ? $(id).value.trim() : $(id).value === '' ? null : +$(id).value);
  if ($('apiKey').value) config.api_key = $('apiKey').value;
  try { const saved = await api('/analysis-settings','POST',config); $('apiKey').value = ''; $('clearKey').checked = false; $('keyState').textContent = saved.has_key ? 'Clave guardada en las credenciales de Windows' : 'Sin clave'; $('settingsState').textContent = 'Configuración guardada'; }
  catch (e) { $('settingsState').textContent = e.message; }
};
$('testApi').onclick = async () => {
  $('testApi').disabled = true; $('settingsState').textContent = 'Probando la configuración guardada…';
  try { const result = await api('/analysis-test','POST'); $('settingsState').textContent = result.text; }
  catch (e) { $('settingsState').textContent = e.message; }
  finally { $('testApi').disabled = false; }
};
$('settingsDialog').addEventListener('close', () => $('apiKey').value = '');

function scheduleRefresh() {
  clearTimeout(refreshTimer); refreshTimer = setTimeout(() => { refreshList().catch(error); if (selected) loadDetail(selected).catch(error); }, 250);
}
function message(data) {
  if (data.type === 'review') { document.dispatchEvent(new CustomEvent('review-updated',{detail:data})); return; }
  if (data.type === 'personal_terms_changed') { document.dispatchEvent(new Event('personal-terms-changed')); return; }
  if (data.type === 'dictionary_changed') { document.dispatchEvent(new Event('dictionary-changed')); return; }
  if (data.type === 'discretion') { setPrivate(data.enabled); return; }
  if (data.type === 'probe') { showProbe(data); return; }
  if (data.type === 'sources') { outputEnabled = data.Otros.enabled; updatePlayback(); return; }
  if (data.type === 'status' && data.state) {
    if (data.revision !== undefined && data.revision < captureRevision) return;
    if (data.revision !== undefined) captureRevision = data.revision;
    captureState = data.state; captureId = data.session_id;
    if ('discreet' in data) setPrivate(data.discreet);
    $('captureState').textContent = captureNames[captureState] || 'Listo';
    $('prepare').disabled = !['ready','finished'].includes(captureState); validateContext(); updatePlayback();
    if (!['ready','finished'].includes(captureState) && $('preparationDialog').open) $('preparationDialog').close();
    scheduleRefresh(); return;
  }
  if (data.type === 'session_deleted') { forgetSession(data.session_id); return; }
  if (data.type === 'analysis') { refreshList().catch(error); if (selected === data.session_id) loadAnalysis(selected).catch(error); return; }
  if (['session_saved','session_updated'].includes(data.type) || (['text','note','markers'].includes(data.type) && selected === data.session_id)) scheduleRefresh();
}
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { captureRevision = -1; };
  ws.onmessage = e => message(JSON.parse(e.data)); ws.onclose = () => { setPrivate(true); setTimeout(connect,1500); };
}
document.addEventListener('visibilitychange', () => { if (document.hidden) player.pause(); });
window.addEventListener('beforeunload', () => { if ($('preparationDialog').open) navigator.sendBeacon('/probe/stop', new Blob(['{}'],{type:'application/json'})); });
Promise.all([api('/info'),api('/sources')]).then(([info,sources]) => { native = info.window; outputEnabled = sources.Otros.enabled; }).catch(error);
refreshList().catch(error); connect();
