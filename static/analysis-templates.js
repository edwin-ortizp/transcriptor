/* Analysis formats (visible prompt, user formats) and one-shot questions about a meeting. */
(() => {
  let listing = null, asking = false;
  const selects = ['analysisTemplate','draftTemplate','templatePick'];
  function fill(keep = {}) {
    for (const id of selects) {
      const select = $(id), value = keep[id] ?? select.value;
      select.replaceChildren();
      for (const [key,item] of Object.entries(listing.templates)) {
        const option = document.createElement('option'); option.value = key;
        option.textContent = item.name + (item.base ? (item.modified ? ' (editado)' : '') : ' · propio');
        select.append(option);
      }
      select.value = listing.templates[value] ? value : 'meeting';
    }
  }
  async function load(keep) { listing = await api('/analysis-templates'); fill(keep); }
  window.loadTemplates = load;
  load().catch(error);
  // The session detail sets the select before formats load; keep it once they arrive.
  document.addEventListener('session-detail-loaded', e => { if (listing && listing.templates[e.detail.template]) $('analysisTemplate').value = e.detail.template; });

  function prompt() {
    const sections = $('templateSections').value.trim() || '…';
    return `${listing.system}\nEstructura requerida: ${sections}\n${listing.stages.final.replace(/^Estructura requerida: \{formato\}\n?/,'')}`;
  }
  function show(id, fresh = false) {
    const item = listing.templates[id];
    $('templatePick').value = id; $('templateError').textContent = '';
    $('templateName').value = fresh ? '' : item.name; $('templateName').disabled = item.base && !fresh;
    if (!fresh) $('templateSections').value = item.sections;
    $('templateHint').textContent = item.base ? (item.modified ? 'Formato base editado. "Restaurar" vuelve al texto original.' : 'Formato base. Puedes ajustar su texto o guardarlo como formato nuevo.') : 'Formato propio.';
    $('templateDelete').textContent = item.base ? 'Restaurar original' : 'Eliminar';
    $('templateDelete').disabled = item.base && !item.modified;
    $('templatePrompt').textContent = prompt();
  }
  window.openTemplate = (sections = null) => {
    if (!listing) return;
    $('templateDialog').showModal(); show($('analysisTemplate').value);
    if (sections !== null) { $('templateName').disabled = false; $('templateName').value = ''; $('templateSections').value = sections; $('templatePrompt').textContent = prompt(); $('templateName').focus(); }
  };
  $('editTemplate').onclick = () => window.openTemplate();
  $('templatePick').onchange = () => show($('templatePick').value);
  $('templateSections').oninput = () => { $('templatePrompt').textContent = prompt(); };
  async function done(id, message) { await load({analysisTemplate:id, templatePick:id}); show(id); notify(message); }
  $('templateForm').onsubmit = async e => {
    e.preventDefault(); const id = $('templatePick').value;
    try { await api(`/analysis-templates/${id}`,'PATCH',{name:$('templateName').value, sections:$('templateSections').value}); await done(id,'Formato guardado. Regenera el análisis para aplicarlo.'); }
    catch (err) { $('templateError').textContent = err.message; }
  };
  $('templateSaveNew').onclick = async () => {
    const name = $('templateName').disabled || !$('templateName').value.trim() ? window.prompt('Nombre del formato nuevo') : $('templateName').value;
    if (!name) return;
    try { const data = await api('/analysis-templates','POST',{name, sections:$('templateSections').value}); await done(data.id,'Formato creado y seleccionado.'); }
    catch (err) { $('templateError').textContent = err.message; }
  };
  $('templateDelete').onclick = async () => {
    const id = $('templatePick').value, item = listing.templates[id];
    if (!item.base && !confirm(`¿Eliminar el formato "${item.name}"?`)) return;
    try { await api(`/analysis-templates/${id}`,'DELETE'); const next = listing.templates[id]?.base ? id : 'meeting'; await done(next, item.base ? 'Formato restaurado.' : 'Formato eliminado.'); }
    catch (err) { $('templateError').textContent = err.message; }
  };

  window.renderQuestions = (questions = []) => {
    $('questionList').replaceChildren();
    [...questions].reverse().forEach(item => {
      const box = document.createElement('div'); box.className = 'question-item';
      const title = document.createElement('strong'); title.textContent = item.question;
      const answer = document.createElement('div'); renderAnalysis(item.answer, answer);
      const actions = document.createElement('div'); actions.className = 'question-actions';
      const meta = document.createElement('span'); meta.className = 'hint';
      meta.textContent = `${dateText(item.created_at)} · ${item.basis === 'notes' ? 'con las notas del análisis' : 'con la transcripción completa'}`;
      const save = document.createElement('button'); save.type = 'button'; save.textContent = 'Guardar como formato';
      save.onclick = () => window.openTemplate(`Responde: ${item.question}`);
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Eliminar';
      remove.onclick = async () => {
        try { await api(`/sessions/${selected}/questions/${item.id}`,'DELETE'); await loadAnalysis(selected); } catch (e) { error(e); }
      };
      actions.append(meta, save, remove); box.append(title, answer, actions); $('questionList').append(box);
    });
  };
  $('askForm').onsubmit = async e => {
    e.preventDefault(); if (privateView || !selected || asking) return;
    const sid = selected, question = $('questionText').value.trim(); if (!question) return;
    asking = true; $('askButton').disabled = true; $('askButton').textContent = 'Preguntando…'; $('questionError').textContent = '';
    try { await api(`/sessions/${sid}/questions`,'POST',{question}); $('questionText').value = ''; if (selected === sid) await loadAnalysis(sid); }
    catch (err) { $('questionError').textContent = err.message; }
    finally { asking = false; $('askButton').disabled = false; $('askButton').textContent = 'Preguntar'; }
  };
})();
