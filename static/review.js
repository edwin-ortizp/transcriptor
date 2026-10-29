(() => {
  let review=null,page=1,pages=1,requestNumber=0,timer,localBusy=false,localError='';
  const names={pending:'Pendiente',ignored:'Ignorada en esta reunión',accepted:'Agregada al diccionario personal',mapped:'Mapping creado'};
  window.resetSuspects=()=>{review=null;page=1;localBusy=false;localError='';++requestNumber;$('reviewRows').replaceChildren();$('reviewError').textContent='';$('suspectCount').textContent='';};
  async function load(){
    if(!selected)return;
    const sid=selected,ticket=++requestNumber;
    if(Array.from($('reviewSearch').value).length>200){$('reviewError').textContent='La búsqueda admite hasta 200 caracteres; se conserva el texto.';return;}
    try{
      const data=await api(`/sessions/${sid}/suspects?status=${$('reviewStatus').value}&q=${encodeURIComponent($('reviewSearch').value)}&page=${page}`);
      if(sid!==selected||ticket!==requestNumber)return;
      review=data;render();
    }catch(e){if(sid===selected&&ticket===requestNumber){$('reviewError').textContent=e.message;$('scanWords').disabled=localBusy||detail?.state!=='finished';}}
  }
  window.loadSuspects=load;
  function render(){
    $('scanWords').disabled=localBusy||review.scanning||detail?.state!=='finished';
    $('scanWords').textContent=localBusy||review.scanning?'Buscando…':'Buscar palabras';
    $('suspectCount').textContent=review.state==='idle'?'':`(${review.pending})`;
    $('reviewState').textContent=review.scanning?'Buscando términos localmente…':review.state==='idle'?'Pulsa Buscar palabras para revisar esta sesión.':`${review.pending} pendientes · ${review.reviewed} revisadas${review.updated_at?' · '+dateText(review.updated_at):''}`;
    if(detail?.state!=='finished')$('reviewState').textContent='La búsqueda estará disponible al terminar esta sesión.';
    $('reviewStale').hidden=!review.stale;$('reviewError').textContent=localError||review.error||review.warning||'';
    $('reviewRows').replaceChildren();
    for(const row of review.rows){
      const card=document.createElement('article');card.className='review-card';card.dataset.suspectId=row.id;
      const head=document.createElement('div');head.className='review-card-heading';
      const title=document.createElement('strong');title.textContent=row.term;
      const frequency=document.createElement('span');frequency.className='badge';frequency.textContent=`${row.count} ${row.count===1?'aparición':'apariciones'}`;
      head.append(title,frequency);card.append(head);
      const variants=document.createElement('div');variants.className='hint';variants.textContent=`${names[row.status]} · Variantes: ${Object.keys(row.variants).join(', ')}`;card.append(variants);
      const contexts=document.createElement('div');contexts.className='review-contexts';let shown=0;
      function more(){
        const next=row.occurrences.slice(shown,shown+3);shown+=next.length;
        next.forEach(occurrence=>{
          const button=document.createElement('button');button.className='review-context';
          button.textContent=`${occurrence.time} · ${occurrence.source} · ${occurrence.context}`;
          button.onclick=()=>{
            showTab('transcript');seek(occurrence.start);
            const block=document.querySelector(`[data-event-id="${CSS.escape(occurrence.fragment_id)}"]`);
            document.querySelectorAll('.review-focus').forEach(el=>el.classList.remove('review-focus'));
            if(block){block.classList.add('review-focus');block.scrollIntoView({block:'center'});}
          };
          contexts.append(button);
        });
        moreButton.hidden=shown>=row.occurrences.length;
      }
      const moreButton=document.createElement('button');moreButton.className='review-more';moreButton.textContent='Ver más apariciones';moreButton.onclick=more;more();card.append(contexts,moreButton);
      const actions=document.createElement('div');actions.className='button-row';
      function action(label,handler){const button=document.createElement('button');button.textContent=label;button.disabled=review.scanning||localBusy;button.onclick=handler;actions.append(button);}
      if(row.status==='pending'){
        action('Agregar al diccionario personal',()=>decide(row,'accept'));
        action('Ignorar en esta reunión',()=>decide(row,'ignore'));
        action('Crear mapping',()=>window.openDictionary({view:'mappings',mappingSource:row.term,reviewContext:{sid:selected,id:row.id}}));
      }else if(row.status==='ignored')action('Deshacer ignorar',()=>decide(row,'restore'));
      else if(row.status==='accepted')action('Gestionar mis términos',()=>window.openDictionary({view:'personal'}));
      else action('Ver mappings',()=>window.openDictionary({view:'mappings'}));
      card.append(actions);$('reviewRows').append(card);
    }
    if(!review.rows.length&&review.state!=='idle'){
      const empty=document.createElement('p');empty.className='hint';empty.textContent=review.total?'No hay términos en esta página.':'No hay palabras para este filtro.';$('reviewRows').append(empty);
    }
    pages=Math.max(1,Math.ceil(review.total/review.page_size));$('reviewPage').textContent=`${review.total} términos · Página ${page} de ${pages}`;
    $('reviewPrev').disabled=page<=1;$('reviewNext').disabled=page>=pages;
  }
  async function decide(row,action){
    const sid=selected,revision=review.revision;
    try{await api(`/sessions/${sid}/suspects/${row.id}/decision`,'POST',{revision,action});await load();}
    catch(e){$('reviewError').textContent=e.message;}
  }
  $('scanWords').onclick=async()=>{
    if(!selected)return;
    const sid=selected;localBusy=true;localError='';$('scanWords').disabled=true;$('scanWords').textContent='Buscando…';$('reviewError').textContent='';
    try{await api(`/sessions/${sid}/suspects/search`,'POST');}
    catch(e){localError=e.message;$('reviewError').textContent=e.message;}
    finally{localBusy=false;await load();}
  };
  $('reviewStatus').onchange=()=>{page=1;load();};
  $('reviewSearch').oninput=()=>{clearTimeout(timer);page=1;timer=setTimeout(load,200);};
  $('reviewPrev').onclick=()=>{if(page>1){page--;load();}};
  $('reviewNext').onclick=()=>{if(page<pages){page++;load();}};
  document.addEventListener('review-updated',event=>{
    const data=event.detail;if(data.session_id!==selected)return;
    if(data.state==='scanning'&&data.total)$('reviewState').textContent=`Revisando bloques: ${data.checked}/${data.total}`;
    load();
  });
  document.addEventListener('personal-terms-changed',load);
  document.addEventListener('dictionary-changed',load);
  document.addEventListener('review-mapping-saved',async event=>{
    const context=event.detail;
    try{
      const data=await api(`/sessions/${context.sid}/suspects`);
      await api(`/sessions/${context.sid}/suspects/${context.id}/decision`,'POST',{revision:data.revision,action:'mapping'});
      $('mappingStatus').textContent+=' · Término atendido en la revisión; el texto original se conserva.';
      if(context.sid===selected)await load();
    }catch(e){$('mappingStatus').textContent+=' · '+e.message;}
  });
})();
