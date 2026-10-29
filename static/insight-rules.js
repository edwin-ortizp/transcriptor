/* Insight rules editor: add phrases, disable noisy rules, manage exclusions and AI suggestions. */
(() => {
  const TYPES = {decision:'Decisión', commitment:'Compromiso', risk:'Riesgo', question:'Pregunta'};
  const ORIGINS = {base:'App', user:'Mía', suggested:'Sugerida'};
  let data = null;
  async function send(url, method = 'GET', body) {
    const r = await fetch(url, {method, headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined});
    const result = await r.json();
    if (!r.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Revisa los campos');
    return result;
  }
  const el = (tag, text, props = {}) => Object.assign(document.createElement(tag), {textContent:text ?? '', ...props});
  async function act(promise, message) {
    $('insightRulesError').textContent = ''; $('insightRulesMessage').textContent = '';
    try { data = await promise; render(); if (message) $('insightRulesMessage').textContent = message; return true; }
    catch (e) { $('insightRulesError').textContent = e.message; return false; }
  }
  function render() {
    $('insightRulesPath').textContent = data.user_path ? `Tus cambios se guardan en ${data.user_path}` : '';
    const query = $('insightRulesSearch').value.trim().toLowerCase(), flagged = $('insightRulesFlagged').checked;
    const rows = data.rules.filter(r => (!flagged || r.flagged) && (!query || (r.phrase || r.pattern || '').includes(query) || TYPES[r.type].toLowerCase().includes(query)));
    $('insightRulesRows').replaceChildren(...rows.map(rule => {
      const tr = el('tr'); tr.classList.toggle('rule-off', !rule.enabled);
      const toggle = el('input', '', {type:'checkbox', checked:rule.enabled, title:rule.enabled ? 'Desactivar' : 'Activar'});
      toggle.onchange = () => act(send('/insight-rules/' + rule.id, 'PATCH', {enabled:toggle.checked}));
      const weight = el('select'); weight.style.width = '64px';
      for (let w = 0; w <= 5; w++) weight.append(el('option', String(w), {value:w, selected:w === rule.weight}));
      weight.onchange = () => act(send('/insight-rules/' + rule.id, 'PATCH', {weight:+weight.value}));
      const s = rule.stats, usage = el('td', `${s.hits} · ${s.pinned} · ${s.dismissed}`);
      if (rule.flagged) usage.append(el('div', 'Muchos falsos positivos', {className:'insight-flag'}));
      const actions = el('td', '', {className:'mapping-actions'});
      if (rule.origin !== 'base') {
        const remove = el('button', 'Eliminar');
        remove.onclick = () => confirm(`¿Eliminar la regla «${rule.phrase}»?`) && act(send('/insight-rules/' + rule.id, 'DELETE'), 'Regla eliminada');
        actions.append(remove);
      }
      const cells = [el('td'), el('td', TYPES[rule.type]), el('td', rule.phrase || `/${rule.pattern}/`), el('td'), el('td', ORIGINS[rule.origin] || rule.origin), usage, actions];
      cells[0].append(toggle); cells[3].append(weight);
      tr.append(...cells); return tr;
    }));
    if (!rows.length) { const tr = el('tr'), td = el('td', 'No hay reglas para mostrar.'); td.colSpan = 7; tr.append(td); $('insightRulesRows').append(tr); }
    $('insightExclusions').replaceChildren(...data.exclusions.map(phrase => {
      const chip = el('button', phrase + ' ×', {className:'insight-chip', title:'Quitar exclusión'});
      chip.onclick = () => act(send('/insight-rules/exclusions/remove', 'POST', {phrase}), 'Exclusión quitada');
      return chip;
    }));
    $('insightSuggestionsBox').hidden = !data.suggestions.length;
    $('insightSuggestions').replaceChildren(...data.suggestions.map(item => {
      const box = el('div', '', {className:'insight-suggestion'});
      box.append(el('div', `${TYPES[item.type]}${item.ref ? ' · ' + item.ref : ''}`, {className:'hint'}), el('div', item.text));
      const row = el('div', '', {className:'button-row'});
      const phrase = el('input', '', {placeholder:'Frase clave de este momento', maxLength:120});
      const accept = el('button', 'Crear regla', {className:'primary'}), reject = el('button', 'Descartar');
      accept.onclick = () => phrase.value.trim() && act(send('/insight-rules', 'POST', {type:item.type, phrase:phrase.value, weight:2, suggestion_id:item.id}), 'Regla creada');
      reject.onclick = () => act(send('/insight-rules/suggestions/' + item.id, 'DELETE'));
      row.append(phrase, accept, reject); box.append(row); return box;
    }));
  }
  window.loadInsightRules = () => act(send('/insight-rules'));
  $('insightRulesSearch').oninput = () => data && render();
  $('insightRulesFlagged').onchange = () => data && render();
  $('insightRuleForm').onsubmit = async event => {
    event.preventDefault();
    if (await act(send('/insight-rules', 'POST', {type:$('insightRuleType').value, phrase:$('insightRulePhrase').value, weight:+$('insightRuleWeight').value}), 'Regla agregada. Aplica desde la próxima frase transcrita.'))
      $('insightRulePhrase').value = '';
  };
  $('insightExclusionForm').onsubmit = async event => {
    event.preventDefault();
    if (await act(send('/insight-rules/exclusions', 'POST', {phrase:$('insightExclusion').value}), 'Exclusión agregada')) $('insightExclusion').value = '';
  };
  $('insightTest').onclick = async () => {
    try {
      const {result} = await send('/insight-rules/test', 'POST', {text:$('insightTestText').value});
      $('insightTestResult').textContent = !result ? 'No genera insight.'
        : `${TYPES[result.type]} · puntaje ${result.score}${result.score < 2 ? ' (por debajo del mínimo para mostrarse solo)' : ''}\nCoincide con: ${result.phrases.join(', ')}`;
    } catch (e) { $('insightTestResult').textContent = e.message; }
  };
  $('insightReplay').onclick = async () => {
    $('insightTestResult').textContent = 'Revisando sesiones…';
    try {
      const r = await send('/insight-rules/replay', 'POST', {limit:10});
      $('insightTestResult').textContent = `${r.total} insights locales en ${r.sessions.length} sesiones (repite la prueba tras un cambio para comparar).\n\n` +
        r.sessions.map(s => `${s.title || s.session_id}: ${s.count}` + s.examples.map(e => `\n   [${e.time}] ${TYPES[e.type]}: ${e.text}`).join('')).join('\n\n');
    } catch (e) { $('insightTestResult').textContent = e.message; }
  };
})();
