/* Delete a saved session after explicit confirmation. Files go to the Windows Recycle Bin. */
(() => {
  function refresh() {
    $('deleteSession').disabled = privateView || !detail || detail.session_id !== selected || detail.state !== 'finished';
  }
  document.addEventListener('session-detail-loaded', refresh);
  document.addEventListener('privacy-changed', refresh);
  $('deleteSession').onclick = () => {
    if (privateView || detail?.session_id !== selected) return;
    $('deleteTitle').textContent = detail.title;
    const audio = (detail.parts || []).filter(p => p.available).length;
    $('deleteMeta').textContent = `${dateText(detail.date)} · ${stamp(detail.duration)}` + (audio ? ` · ${audio} archivo(s) de audio` : ' · sin audio');
    $('deleteError').textContent = ''; $('confirmDelete').disabled = false;
    $('deleteDialog').showModal(); $('cancelDelete').focus();  // the safe choice gets the focus
  };
  $('cancelDelete').onclick = () => $('deleteDialog').close();
  $('deleteForm').onsubmit = async event => {
    event.preventDefault();
    const sid = selected;
    $('confirmDelete').disabled = true;
    try {
      await api('/sessions/' + encodeURIComponent(sid), 'DELETE');
      $('deleteDialog').close();
      forgetSession(sid);
      notify('Reunión enviada a la Papelera de reciclaje.');
    } catch (e) { $('deleteError').textContent = e.message; $('confirmDelete').disabled = false; }
  };
})();
