(() => {
  let revision='missing', page=1, pages=1, requestNumber=0, timer, draftRevision=null;
  async function send(url,method='GET',body){
    const r=await fetch(url,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
    const result=await r.json();if(!r.ok)throw Error(typeof result.detail==='string'?result.detail:'Revisa los campos');return result;
  }
  async function load(){
    const ticket=++requestNumber;
    if(Array.from($('personalTermsSearch').value).length>200){$('personalTermsError').textContent='La búsqueda admite hasta 200 caracteres; se conserva el texto.';return;}
    try{
      const data=await send(`/dictionary/personal-terms?q=${encodeURIComponent($('personalTermsSearch').value)}&page=${page}`);
      if(ticket!==requestNumber)return;
      revision=data.revision;
      $('personalTermsPath').textContent=`${data.path} · ${data.entries_count} términos personales`;
      $('personalTermsRows').replaceChildren();
      for(const entry of data.entries){
        const row=document.createElement('tr');row.dataset.personalTermId=entry.id;
        const term=document.createElement('td');term.textContent=entry.term;
        const actions=document.createElement('td');const remove=document.createElement('button');remove.textContent='Eliminar';
        remove.onclick=async()=>{
          if(!confirm(`¿Eliminar «${entry.term}» del vocabulario personal? Puede volver a aparecer en futuras búsquedas.`))return;
          try{await send(`/dictionary/personal-terms/${entry.id}`,'DELETE',{revision});await load();$('personalTermsMessage').textContent='Término eliminado. Se creó un respaldo.';}
          catch(e){$('personalTermsError').textContent=e.message;}
        };
        actions.append(remove);row.append(term,actions);$('personalTermsRows').append(row);
      }
      if(!data.entries.length){const row=document.createElement('tr');const td=document.createElement('td');td.colSpan=2;td.textContent='No hay términos personales para mostrar.';row.append(td);$('personalTermsRows').append(row);}
      pages=Math.max(1,Math.ceil(data.total/data.page_size));$('personalTermsPage').textContent=`${data.total} resultados · Página ${page} de ${pages}`;
      $('personalTermsPrev').disabled=page<=1;$('personalTermsNext').disabled=page>=pages;
      if(!$('personalTerm').value)draftRevision=revision;
      return true;
    }catch(e){$('personalTermsError').textContent=e.message;return false;}
  }
  window.loadPersonalTerms=load;
  $('personalTerm').addEventListener('input',()=>{if(draftRevision===null)draftRevision=revision;});
  $('personalTermForm').onsubmit=async event=>{
    event.preventDefault();const term=$('personalTerm').value;
    if(!term.trim()||/[\t\r\n\v\f\x85\u2028\u2029]/.test(term)){$('personalTermsError').textContent='Introduce un término sin tabulaciones ni saltos de línea.';return;}
    $('savePersonalTerm').disabled=true;
    try{
      const result=await send('/dictionary/personal-terms','POST',{term,revision:draftRevision??revision});
      $('personalTerm').value='';draftRevision=null;await load();$('personalTermsError').textContent='';
      $('personalTermsMessage').textContent=result.backup?'Término agregado. Respaldo: '+result.backup:'Término agregado al vocabulario personal.';
    }catch(e){$('personalTermsError').textContent=e.message;}
    finally{$('savePersonalTerm').disabled=false;}
  };
  $('reloadPersonalTerms').onclick=async()=>{if(!await load())return;draftRevision=revision;$('personalTermsError').textContent='';$('personalTermsMessage').textContent='Vocabulario recargado; se conserva el término que estabas escribiendo.';};
  $('personalTermsSearch').oninput=()=>{clearTimeout(timer);page=1;timer=setTimeout(load,200);};
  $('personalTermsPrev').onclick=()=>{if(page>1){page--;load();}};
  $('personalTermsNext').onclick=()=>{if(page<pages){page++;load();}};
  document.addEventListener('personal-terms-changed',()=>{
    if($('dictionaryDialog').open&&!$('dictionaryPersonal').hidden){load();$('personalTermsMessage').textContent='El vocabulario cambió. Recarga antes de guardar si tenías un borrador.';}
  });
})();
