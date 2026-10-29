/* Explicit session mutations. Previewing never changes the files. */
(() => {
  let target = null, revision = null, preview = null, operation = 'mappings', page = 0, loading = false;
  const controls = ['renameSession','reapplyMappings','restoreTranscript'];
  async function refresh() {
    controls.forEach(id => $(id).disabled = true);
    if (!detail || detail.session_id !== selected || detail.state !== 'finished' || privateView) return;
    const sid = selected;
    try {
      const info = await api(`/sessions/${sid}/editing`);
      if (sid !== selected || privateView) return;
      $('renameSession').disabled = $('reapplyMappings').disabled = false;
      $('restoreTranscript').disabled = !info.can_restore;
    } catch (_) { /* Operations display the actionable error when opened. */ }
  }
  document.addEventListener('session-detail-loaded',refresh);
  document.addEventListener('privacy-changed',refresh);
  function validateTitle() {
    const value = $('savedTitle').value;
    const count = Array.from(value.trim()).length;
    const invalid = count < 1 || count > 120 || /[\p{Cc}\p{Cs}]/u.test(value);
    $('savedTitleHint').textContent = `${count}/120 caracteres. Los archivos se renombran con el nuevo título; la fecha se conserva.`;
    $('savedTitleHint').classList.toggle('error',invalid); $('savedTitle').setAttribute('aria-invalid',String(invalid));
    $('saveRename').disabled = invalid || loading;
    return !invalid;
  }
  $('savedTitle').oninput = validateTitle;
  $('renameSession').onclick = async () => {
    if (privateView || detail?.session_id !== selected) return;
    target = selected;
    try {
      const info = await api(`/sessions/${target}/editing`);
      if (privateView || target !== selected) return;
      revision = info.revision; $('savedTitle').value = info.title; $('renameError').textContent = '';
      validateTitle(); $('renameDialog').showModal(); $('savedTitle').focus();
    } catch (e) { error(e); }
  };
  $('reloadRename').onclick = async () => {
    try { revision = (await api(`/sessions/${target}/editing`)).revision; $('renameError').textContent = 'Datos recargados. Tu título propuesto se conserva; revisa y guarda.'; }
    catch (e) { $('renameError').textContent = e.message; }
  };
  $('renameForm').onsubmit = async e => {
    e.preventDefault(); if (privateView || !validateTitle()) return;
    loading = true; validateTitle();
    try {
      const saved = await api(`/sessions/${target}/title`,'PATCH',{title:$('savedTitle').value,revision});
      $('renameDialog').close(); await refreshList(); if (selected === target) await loadDetail(target);
      notify(saved.files_warning || `Título guardado. Archivo: ${(saved.txt_path || '').split(/[\\/]/).pop()}`);
    } catch (e) { $('renameError').textContent = e.message; }
    finally { loading = false;validateTitle(); }
  };
  function renderPreview() {
    const changes = preview?.changes || [];
    $('textEditChanges').replaceChildren();
    changes.slice(page*10,(page+1)*10).forEach(change => {
      const box = document.createElement('section'); box.className = 'text-change';
      const heading = document.createElement('h3'); heading.textContent = `[${change.time}] ${change.source}`;
      box.append(heading);
      for (const [label,text] of [['Anterior',change.before],['Propuesto',change.after]]) {
        const title = document.createElement('strong');title.textContent = label;
        const content = document.createElement('pre');content.textContent = text || '(vacío)';box.append(title,content);
      }
      $('textEditChanges').append(box);
    });
    $('textEditCount').textContent = `${changes.length} bloques modificados${changes.length ? '. Se guardará un respaldo antes de reemplazar.' : '. No hay cambios que guardar.'}`;
    $('editPage').textContent = `${page+1} / ${Math.max(1,Math.ceil(changes.length/10))}`;
    $('editPrev').disabled = page===0; $('editNext').disabled = (page+1)*10 >= changes.length;
    $('confirmTextEdit').disabled = loading || !changes.length;
  }
  async function calculate() {
    loading = true; preview = null; page = 0;
    $('confirmTextEdit').disabled = true; $('recalculateEdit').disabled = true;
    $('textEditError').textContent = ''; $('textEditCount').textContent = 'Calculando vista previa…';
    $('textEditChanges').replaceChildren(); $('textEditFiles').replaceChildren();
    try {
      const data = await api(`/sessions/${target}/text-preview`,'POST',{operation});
      if (privateView || !$('textEditDialog').open) return;
      preview = data;
      $('textEditNotice').textContent = operation === 'restore' ? 'Restaurar solo cambia el texto transcrito. Conserva el título, los marcadores y el audio actuales.' : data.base_notice;
      for (const file of data.files) { const p = document.createElement('div'); p.textContent = `${file.path} · ${file.rules_count} reglas`; $('textEditFiles').append(p); }
      if (data.warnings.length) { const p = document.createElement('p'); p.textContent = `${data.warnings.length} líneas malformadas no se usan como reglas. Puedes revisarlas en Diccionario.`; $('textEditFiles').append(p); }
    } catch (e) { $('textEditError').textContent = e.message; }
    finally { loading = false; $('recalculateEdit').disabled = false; renderPreview(); }
  }
  async function openPreview(kind) {
    if (privateView || detail?.session_id !== selected) return;
    target = selected; operation = kind;
    $('textEditHeading').textContent = kind === 'restore' ? 'Restaurar texto anterior' : 'Reaplicar mappings';
    $('textEditNotice').textContent = ''; $('textEditDialog').showModal(); await calculate();
  }
  $('reapplyMappings').onclick = () => openPreview('mappings');
  $('restoreTranscript').onclick = () => openPreview('restore');
  $('recalculateEdit').onclick = calculate;
  $('editPrev').onclick = () => { page--;renderPreview(); };
  $('editNext').onclick = () => { page++;renderPreview(); };
  $('textEditForm').onsubmit = async e => {
    e.preventDefault(); if (privateView || loading || !preview?.changes.length) return;
    loading = true; $('confirmTextEdit').disabled = true;
    try {
      await api(`/sessions/${target}/text-apply`,'POST',{token:preview.token});
      $('textEditDialog').close(); await refreshList(); if (selected === target) await loadDetail(target);
      notify('Texto actualizado. El respaldo está guardado; revisa el análisis y las palabras sospechosas.');
    } catch (e) { $('textEditError').textContent = e.message; }
    finally { loading = false; if ($('textEditDialog').open) renderPreview(); }
  };
  // These dialogs are inserted before manager.js wires the shared close controls.
})();
