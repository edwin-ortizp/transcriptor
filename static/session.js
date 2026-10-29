/* Recording widget only. Session management lives in /manager. */
window.name = 'transcriptor-widget';
let privateView = true;
let geometryQueue = Promise.resolve(), lastGeometry = null;
function setPrivate(enabled) {
  privateView = enabled;
  if (enabled) {
    closeMenu(); $('settings').classList.remove('open'); $('gear').classList.remove('active');
    $('title').blur(); document.getSelection()?.removeAllRanges(); document.activeElement?.blur();
  }
  document.body.classList.toggle('discreet', enabled);
  $('discretion').setAttribute('aria-pressed', String(enabled));
  $('discretion').title = enabled ? 'Mostrar contenido' : 'Ocultar contenido';
  $('discretion').setAttribute('aria-label', $('discretion').title);
  if (window.pywebview?.api && lastGeometry !== enabled) {
    lastGeometry = enabled;
    geometryQueue = geometryQueue.then(() => window.pywebview.api.set_widget_compact(enabled)).catch(() => { lastGeometry = null; });
  }
}
window.addEventListener('pywebviewready', () => setPrivate(privateView));
window.widgetConnectionLost = () => setPrivate(true);
$('discretion').onclick = async () => { setPrivate(!privateView); await post('/discretion', {enabled:privateView}); };
document.addEventListener('copy', e => { if (privateView) e.preventDefault(); });
$('sessions').onclick = async () => {
  if (privateView) return;
  // Open synchronously to preserve the browser's user gesture and reuse the named tab.
  if (!isWindow) {
    const target = window.open('', 'transcriptor-sessions');
    if (target) { try { if (target.location.href === 'about:blank') target.location.href = '/manager'; target.focus(); } catch (_) { window.open('/manager', 'transcriptor-sessions'); } }
    return;
  }
  await post('/show-manager');
};
let backupOn = false;
function showBackup(m) {
  const between = ['ready', 'finished'].includes(m.state);
  backupOn = between ? !!m.backup_next : !!m.backup_active;
  const failed = !between && !!m.backup_error;
  const b = $('backup');
  b.classList.toggle('on', backupOn); b.classList.toggle('failed', failed);
  b.setAttribute('aria-pressed', String(backupOn));
  b.disabled = !['ready', 'finished', 'recording', 'paused'].includes(m.state);
  b.title = failed ? `Respaldo de audio con error: ${m.backup_error}`
    : between ? `Respaldo de audio en la próxima sesión: ${backupOn ? 'sí' : 'no'}`
    : backupOn ? 'Guardando respaldo de audio · clic para detener' : 'Sin respaldo de audio · clic para empezar a guardarlo';
  b.setAttribute('aria-label', b.title);
}
$('backup').onclick = () => post('/backup', {enabled:!backupOn}).then(showBackup).catch(e => { $('status').textContent = e.message; });
$('mark').onclick = () => post('/markers').catch(e => { $('status').textContent = e.message; });
function attachMoment(element, message) {
  if (message.id) element.dataset.eventId = message.id;
  if (typeof message.start === 'number') element.dataset.start = message.start;
}
function insertMoment(element, fallback) {
  const next = [...$('log').children].find(el => el !== element && el.dataset.start !== undefined && +el.dataset.start > +element.dataset.start);
  $('log').insertBefore(element, next || fallback);
}
window.featureStatus = message => {
  if ('discreet' in message) setPrivate(message.discreet);
  if ('backup_active' in message) showBackup(message);
  $('privateState').textContent = ({ready:'Listo', starting:'Iniciando', recording:'Grabando', pausing:'Pausando', paused:'En pausa', finishing:'Terminando', finished:'Finalizada'})[sessionState];
  document.querySelector('header').dataset.privateState = $('privateState').textContent;
  $('mark').disabled = sessionState !== 'recording';
  if (sessionState === 'ready') fetch('/preparation').then(r => r.json()).then(d => { if (sessionState === 'ready' && !editing()) $('title').textContent = d.title; });
};
window.featureMessage = message => {
  if (message.type === 'discretion') { setPrivate(message.enabled); return true; }
  if (message.type === 'preparation') { if (sessionState === 'ready') $('title').textContent = message.title; return true; }
  if (['analysis','session_saved','probe'].includes(message.type)) return true;
  if (message.session_id && message.session_id !== sessionId && message.type !== 'status') return true;
  if (message.type === 'session_updated' && message.title) $('title').textContent = message.title;
  if (['markers','session_updated'].includes(message.type)) {
    if (message.history) { document.querySelectorAll('#log .line, #log .note').forEach(el => el.remove()); message.history.forEach(handleMessage); }
    return true;
  }
  if (message.type === 'backup_error') { $('backup').classList.add('failed'); $('backup').title = 'Respaldo de audio con error: ' + message.text; }
  if (['backup_error','archive_error'].includes(message.type)) { $('status').textContent = message.text; return true; }
  return false;
};
connect();
