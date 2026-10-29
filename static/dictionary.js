/* Read-only system terms and safe per-user TSV editing. No automatic detection. */
(() => {
  let termPage = 1, termPages = 1, termRequest = 0, searchTimer;
  let mappingData = {files:[],rules:[],warnings:[]}, mappingPage = 1, mappingPages = 1, draft = null, mappingReviewContext = null;
  const dialog = $('dictionaryDialog');
  async function request(url, method='GET', body) {
    const response = await fetch(url,{method,headers:body ? {'Content-Type':'application/json'} : {},body:body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) { const failure = Error(typeof data.detail === 'string' ? data.detail : 'Revisa los campos y sus límites'); failure.status=response.status; throw failure; }
    return data;
  }
  const ruleValues = row => [row.source,row.target,row.mode,row.case];
  const sameRule = (a,b) => JSON.stringify(a) === JSON.stringify(b);
  function cell(text) { const td=document.createElement('td');td.textContent=text;return td; }
  window.openDictionary=async ({view=null,mappingSource=null,reviewContext=null}={})=>{
    if(privateView)return;
    player.pause();if(!dialog.open)dialog.showModal();
    mappingReviewContext=reviewContext;
    try{
      await Promise.all([loadTerms(),loadMappings()]);
      if(view)document.querySelector(`[data-dictionary-view="${view}"]`)?.click();
      if(mappingSource!==null){
        openForm();$('mappingSource').value=mappingSource;$('mappingTarget').focus();
      }
    }catch(e){$('mappingStatus').textContent=e.message;}
  };
  $('showDictionary').onclick=()=>window.openDictionary();
  document.querySelectorAll('[data-dictionary-view]').forEach(button=>button.onclick=()=>{
    const view=button.dataset.dictionaryView;
    $('dictionarySystem').hidden=view!=='system';$('dictionaryMappings').hidden=view!=='mappings';$('dictionaryPersonal').hidden=view!=='personal';$('dictionaryInsights').hidden=view!=='insights';
    document.querySelectorAll('[data-dictionary-view]').forEach(b=>b.classList.toggle('active',b===button));
    if(view==='personal')window.loadPersonalTerms?.();
    if(view==='insights')window.loadInsightRules?.();
  });
  async function loadTerms() {
    const number=++termRequest;
    if (Array.from($('termSearch').value).length>200) { $('termError').textContent='La búsqueda admite hasta 200 caracteres; el texto se conserva.';return; }
    try {
      const data=await request(`/dictionary/terms?source=${$('termSource').value}&q=${encodeURIComponent($('termSearch').value)}&page=${termPage}`);
      if(number!==termRequest)return;
      $('termError').textContent='';$('termRows').replaceChildren();
      for(const entry of data.entries){ const row=document.createElement('tr');row.append(cell(entry.term),cell({es:'Español',en:'Inglés',both:'Español / Inglés'}[entry.language]),cell(entry.note));$('termRows').append(row); }
      if(!data.entries.length){const row=document.createElement('tr');const td=cell('No hay términos para esta búsqueda.');td.colSpan=3;row.append(td);$('termRows').append(row);}
      termPages=Math.max(1,Math.ceil(data.total/data.page_size));
      $('termInfo').textContent=`${data.total.toLocaleString('es-CO')} resultados · ${data.entries_count.toLocaleString('es-CO')} entradas de referencia`;
      $('termPage').textContent=`Página ${termPage} de ${termPages}`;$('termPrev').disabled=termPage<=1;$('termNext').disabled=termPage>=termPages;
      $('termProvenance').textContent=data.provenance.version ? `${data.provenance.repository} · versión ${data.provenance.version} · commit ${data.provenance.commit}` : data.provenance.repository;
    }catch(e){if(number===termRequest)$('termError').textContent=e.message;}
  }
  $('termSource').onchange=()=>{termPage=1;loadTerms();};
  $('termSearch').oninput=()=>{clearTimeout(searchTimer);termPage=1;searchTimer=setTimeout(loadTerms,200);};
  $('termPrev').onclick=()=>{if(termPage>1){termPage--;loadTerms();}};
  $('termNext').onclick=()=>{if(termPage<termPages){termPage++;loadTerms();}};

  function renderMappings(){
    $('mappingOrigin').textContent=`Configuración efectiva: ${mappingData.configuration_origin || ''}`;
    $('mappingFiles').replaceChildren();
    for(const file of mappingData.files){const row=document.createElement('div');row.textContent=`${file.path} · ${file.error || (!file.exists ? 'No existe' : !file.editable ? 'Solo lectura' : file.rules_count+' reglas')}`;$('mappingFiles').append(row);}
    $('createPersonal').hidden=!mappingData.can_create_personal;
    $('createPersonal').title=mappingData.personal_path || '';
    $('addMapping').disabled=!mappingData.files.some(f=>f.exists&&f.revision&&f.editable);
    const query=$('mappingSearch').value.toLocaleLowerCase();
    const rows=mappingData.rules.filter(r=>[r.source,r.target,r.file_name].some(x=>x.toLocaleLowerCase().includes(query)));
    mappingPages=Math.max(1,Math.ceil(rows.length/50));mappingPage=Math.min(mappingPage,mappingPages);
    $('mappingRows').replaceChildren();
    for(const rule of rows.slice((mappingPage-1)*50,mappingPage*50)){
      const row=document.createElement('tr');row.dataset.mappingId=rule.id;row.dataset.fileId=rule.file_id;
      const target=cell(rule.target || '(eliminar coincidencia)');if(!rule.target)target.className='muted';
      row.append(cell(rule.source),target,cell(rule.mode==='word'?'Completa':'Cualquier parte'),cell(rule.case==='case'?'Distinguir':'Ignorar'),cell(rule.file_name));
      const actions=document.createElement('td');actions.className='mapping-actions';
      const edit=document.createElement('button');edit.textContent='Editar';edit.disabled=!mappingData.files.find(f=>f.id===rule.file_id)?.editable;edit.onclick=()=>{mappingReviewContext=null;openForm(rule);};
      const remove=document.createElement('button');remove.textContent='Eliminar';remove.disabled=edit.disabled;remove.onclick=()=>deleteRule(rule);
      actions.append(edit,remove);row.append(actions);$('mappingRows').append(row);
    }
    if(!rows.length){const row=document.createElement('tr');const td=cell('No hay mappings para mostrar.');td.colSpan=6;row.append(td);$('mappingRows').append(row);}
    $('mappingPage').textContent=`${rows.length} reglas · Página ${mappingPage} de ${mappingPages}`;
    $('mappingPrev').disabled=mappingPage<=1;$('mappingNext').disabled=mappingPage>=mappingPages;
    $('mappingWarnings').textContent=mappingData.warnings.map(w=>`${w.file_name}, línea ${w.line}: ${w.message}. Se conserva sin modificar.`).join('\n');
  }
  async function loadMappings(rebase=false){
    mappingData=await request('/dictionary/mappings');renderMappings();
    if(rebase&&draft){
      const file=mappingData.files.find(f=>f.id===draft.file_id);
      const matches=draft.original ? mappingData.rules.filter(r=>r.file_id===draft.file_id&&sameRule(ruleValues(r),draft.original)) : [];
      if(file?.revision&&(!draft.original||matches.length===1)){
        draft.revision=file.revision;if(draft.original)draft.row_id=matches[0].id;
        $('saveMapping').disabled=false;$('mappingAsNew').hidden=true;$('mappingError').textContent='Archivo recargado; se conserva tu borrador. Revisa las reglas actuales antes de guardar.';
      }else{
        $('saveMapping').disabled=true;$('mappingAsNew').hidden=false;$('mappingError').textContent='La regla original cambió o desapareció. Tu borrador se conserva; puedes volver a abrir la regla actual o usarlo como una nueva.';
      }
    }
  }
  function openForm(rule=null){
    const available=mappingData.files.filter(f=>f.exists&&f.revision&&f.editable);
    $('mappingFile').replaceChildren(new Option('Elige un archivo…',''));
    available.forEach(file=>$('mappingFile').add(new Option(file.path,file.id)));
    const file=rule ? available.find(f=>f.id===rule.file_id) : available.length===1 ? available[0] : null;
    draft={file_id:file?.id || '',revision:file?.revision || '',row_id:rule?.id || null,original:rule ? ruleValues(rule) : null};
    $('mappingFile').value=draft.file_id;$('mappingFile').disabled=!!rule;
    $('mappingSource').value=rule?.source || '';$('mappingTarget').value=rule?.target || '';
    $('mappingMode').value=rule?.mode || 'word';$('mappingCase').value=rule?.case || 'nocase';
    $('mappingFormTitle').textContent=rule?'Editar mapping':'Agregar mapping';$('mappingForm').hidden=false;
    $('mappingError').textContent='';$('saveMapping').disabled=false;$('mappingAsNew').hidden=true;
    $('mappingSource').focus();$('mappingForm').scrollIntoView({block:'nearest'});
  }
  $('mappingFile').onchange=()=>{const file=mappingData.files.find(f=>f.id===$('mappingFile').value);draft.file_id=file?.id || '';draft.revision=file?.revision || '';$('saveMapping').disabled=!draft.revision;};
  $('addMapping').onclick=()=>{mappingReviewContext=null;openForm();};
  $('cancelMapping').onclick=()=>{draft=null;mappingReviewContext=null;$('mappingForm').hidden=true;};
  $('mappingAsNew').onclick=()=>{
    draft.row_id=null;draft.original=null;$('mappingFile').disabled=false;
    $('mappingFile').replaceChildren(new Option('Elige un archivo…',''));
    mappingData.files.filter(f=>f.exists&&f.revision&&f.editable).forEach(f=>$('mappingFile').add(new Option(f.path,f.id)));
    $('mappingFile').value=draft.file_id;
    const file=mappingData.files.find(f=>f.id===$('mappingFile').value);
    draft.file_id=file?.id || '';draft.revision=file?.revision || '';
    $('saveMapping').disabled=!draft.revision;$('mappingAsNew').hidden=true;$('mappingFormTitle').textContent='Agregar mapping desde borrador';
  };

  $('mappingForm').onsubmit=async event=>{
    event.preventDefault();if(!draft)return;
    const body={revision:draft.revision,source:$('mappingSource').value,target:$('mappingTarget').value,mode:$('mappingMode').value,case:$('mappingCase').value};
    if(!draft.file_id){$('mappingError').textContent='Elige explícitamente el archivo de destino.';return;}
    if(!body.source.trim()||/[\t\r\n\v\f\x85\u2028\u2029]/.test(body.source+body.target)){$('mappingError').textContent='El origen es obligatorio; los campos no admiten tabulaciones ni saltos de línea.';return;}
    $('saveMapping').disabled=true;
    try{
      const suffix=draft.row_id?'/'+draft.row_id:'';
      const result=await request(`/dictionary/mappings/${draft.file_id}/rules${suffix}`,draft.row_id?'PATCH':'POST',body);
      draft=null;$('mappingForm').hidden=true;await loadMappings();$('mappingStatus').textContent='Guardado para próximas sesiones. Respaldo: '+result.backup;
      if(mappingReviewContext){const context=mappingReviewContext;mappingReviewContext=null;document.dispatchEvent(new CustomEvent('review-mapping-saved',{detail:context}));}
    }catch(e){$('mappingError').textContent=e.message;if(e.status===409)$('mappingError').textContent+=' Usa Recargar para revisar los cambios sin perder este borrador.';}
    finally{if(draft)$('saveMapping').disabled=false;}
  };
  async function deleteRule(rule){
    if(!confirm(`¿Eliminar el mapping «${rule.source}»? Se creará un respaldo.`))return;
    const file=mappingData.files.find(f=>f.id===rule.file_id);
    try{const result=await request(`/dictionary/mappings/${file.id}/rules/${rule.id}`,'DELETE',{revision:file.revision});await loadMappings();$('mappingStatus').textContent='Mapping eliminado. Respaldo: '+result.backup;}
    catch(e){$('mappingStatus').textContent=e.message;}
  }
  $('reloadMappings').onclick=()=>loadMappings(true).catch(e=>$('mappingStatus').textContent=e.message);
  $('createPersonal').onclick=async()=>{try{
    await request('/dictionary/mappings/create-personal','POST');await loadMappings();
    if(draft&&!draft.row_id){
      const files=mappingData.files.filter(f=>f.exists&&f.revision&&f.editable);
      $('mappingFile').replaceChildren(new Option('Elige un archivo…',''));files.forEach(f=>$('mappingFile').add(new Option(f.path,f.id)));
      const file=files.length===1?files[0]:null;draft.file_id=file?.id||'';draft.revision=file?.revision||'';
      $('mappingFile').value=draft.file_id;$('mappingFile').disabled=false;$('saveMapping').disabled=!draft.revision;
    }
    $('mappingStatus').textContent='Archivo personal seleccionado. No se trasladaron archivos anteriores.';
  }catch(e){$('mappingStatus').textContent=e.message;}};
  $('mappingSearch').oninput=()=>{mappingPage=1;renderMappings();};
  $('mappingPrev').onclick=()=>{if(mappingPage>1){mappingPage--;renderMappings();}};
  $('mappingNext').onclick=()=>{if(mappingPage<mappingPages){mappingPage++;renderMappings();}};
  $('testMappings').onclick=async()=>{
    const text=$('mappingTestText').value;
    if(Array.from(text).length>20000){$('mappingTestError').textContent='La prueba admite hasta 20.000 caracteres; el texto se conserva.';return;}
    try{const result=await request('/dictionary/mappings/preview','POST',{text});$('mappingTestResult').textContent=result.text;$('mappingTestError').textContent='';}catch(e){$('mappingTestError').textContent=e.message;}
  };
  document.addEventListener('dictionary-changed',()=>{if(dialog.open)$('mappingStatus').textContent='Los mappings se actualizaron. Usa Recargar para consultar su estado actual; tu borrador se conserva.';});
})();
