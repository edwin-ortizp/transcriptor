/* Live insight card: one insight at a time, local rules first, AI only when enabled. */
(() => {
  const LABELS = {decision:'Decisión', commitment:'Compromiso', risk:'Riesgo', question:'Pregunta'};
  const PRIORITY = {decision:0, commitment:0, risk:1, question:2};
  const MODES = {off:'Insights: apagado', local:'Insights: local', ai:'Insights: local + IA'};
  let items = [], current = null, mode = 'off', session = null, aiState = null;
  const card = $('insightCard');

  function queue() {
    return items.filter(i => !i.dismissed && !i.seen && i !== current)
      .sort((a, b) => PRIORITY[a.type] - PRIORITY[b.type] || a.created_at - b.created_at);
  }
  function render() {
    const pinned = items.find(i => i.pinned && !i.dismissed);
    if (!current || current.dismissed) current = pinned || queue()[0] || null;
    card.hidden = !session;
    $('insightMode').textContent = MODES[mode] || MODES.off;
    $('insightMode').title = aiState?.degraded || (mode === 'ai' && aiState ? `Llamadas: ${aiState.calls}/${aiState.max_calls}` : 'Cambiar modo de insights');
    $('insightBody').hidden = !current;
    $('insightNow').disabled = mode === 'off' || !!aiState?.busy;
    $('insightNow').textContent = aiState?.busy ? 'Pensando…' : 'Insight ahora';
    const pending = queue().length;
    $('insightNext').hidden = !pending;
    $('insightNext').textContent = `Siguiente (${pending})`;
    $('insightWrong').hidden = !current || current.source !== 'local';
    if (!current) { $('insightWrongMenu').hidden = true; return; }
    card.dataset.type = current.type;
    $('insightType').textContent = LABELS[current.type] || current.type;
    $('insightSource').textContent = current.source === 'ai' ? 'IA' : 'detectado localmente';
    $('insightText').textContent = current.text;
    $('insightRef').textContent = current.ref || '';
    $('insightRef').hidden = !current.ref;
    $('insightPin').textContent = current.pinned ? 'Soltar' : 'Fijar';
  }
  function upsert(item) {
    const index = items.findIndex(i => i.id === item.id);
    if (index >= 0) items[index] = {...items[index], ...item}; else items.push(item);
    if (current?.id === item.id) current = items.find(i => i.id === item.id);
    render();
  }
  async function load() {
    const data = await fetch('/insights').then(r => r.json());
    session = data.session_id; items = data.items; current = null;
    aiState = data.state; mode = data.state?.mode || 'off';
    render();
  }
  async function change(body) {
    if (!current) return;
    const id = current.id;
    const res = await fetch('/insights/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (res.ok) upsert(await res.json());
  }
  $('insightPin').onclick = () => change({pinned:!current?.pinned});
  $('insightDismiss').onclick = () => change({dismissed:true}).then(() => { current = null; render(); });
  $('insightWrong').onclick = () => { $('insightWrongMenu').hidden = !$('insightWrongMenu').hidden; };
  document.querySelectorAll('#insightWrongMenu [data-feedback]').forEach(button => button.onclick = async () => {
    if (!current) return;
    const kind = button.dataset.feedback, body = {kind};
    if (kind === 'exclude') {
      // Prefill with the matched phrase; the user can widen it ("no hay problema") to avoid hiding real cases.
      const phrase = prompt('Frase que no debe generar insights:', (current.phrases || [])[0] || '');
      if (!phrase) return;
      body.phrase = phrase;
    }
    if (kind === 'disable_rule' && !current.rule_ids?.length) { $('status').textContent = 'Este insight no viene de una regla local'; return; }
    const res = await fetch(`/insights/${current.id}/feedback`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await res.json();
    if (!res.ok) { $('status').textContent = data.detail; return; }
    $('insightWrongMenu').hidden = true;
    upsert(data); current = null; render();
    $('status').textContent = {false_positive:'Registrado como falso positivo', exclude:'Frase ignorada en próximas detecciones', disable_rule:'Regla desactivada'}[kind];
  });
  $('insightNext').onclick = () => { if (current) current.seen = true; current = null; render(); };
  $('insightRef').onclick = () => {
    const target = current && [...document.querySelectorAll('#log [data-start]')].find(el => current.start != null && +el.dataset.start >= current.start - .5);
    target?.scrollIntoView({block:'center'});
  };
  $('insightNow').onclick = () => post('/insights/now').catch(e => { $('status').textContent = e.message; });
  $('insightMode').onclick = async () => {
    const next = {off:'local', local:'ai', ai:'off'}[mode];
    const res = await fetch('/insights/mode', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:next})});
    if (res.ok) { aiState = await res.json(); mode = aiState.mode; render(); }
    else {
      // Without AI available the cycle skips straight to off; local mode keeps working.
      $('status').textContent = (await res.json()).detail;
      if (next === 'ai') await fetch('/insights/mode', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'off'})})
        .then(r => r.ok ? r.json() : null).then(s => { if (s) { aiState = s; mode = s.mode; render(); } });
    }
  };

  const previousMessage = window.featureMessage;
  window.featureMessage = m => {
    if (m.type === 'insight') { if (m.session_id === session) upsert(m.insight); return true; }
    if (m.type === 'insights_state') { if (m.session_id === session) { aiState = m; mode = m.mode; render(); } return true; }
    if (m.type === 'status' && m.session_id !== session) load();
    return previousMessage ? previousMessage(m) : false;
  };
  const defaults = $('insightDefault');
  fetch('/insights-settings').then(r => r.json()).then(c => { defaults.value = c.mode; });
  defaults.onchange = () => post('/insights-settings', {mode:defaults.value}).catch(e => { $('status').textContent = e.message; });
  load();
})();
