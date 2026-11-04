"""Session mutations use temporary synthetic files, never personal transcripts."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import session_edits as edits
from mapping_store import MappingStore, MappingConflict
from recording import atomic_json
from session_repository import SessionRepository
from test_analysis import make_session


class SessionEditingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = make_session(self.root,text='alfa beta ÁRBOL')
        self.repo = SessionRepository(lambda:self.root,lambda:None,self.root/'index.json')
        self.sid = self.repo.register(self.path)['session_id']
        self.mapping = self.root/'mappings.tsv'
        self.mapping.write_text('alfa\tBeta\tword\tnocase\nárbol\tÁrbol técnico\tword\tnocase\n',encoding='utf-8')
        self.files = [self.mapping]
        self.mappings = MappingStore(lambda:self.files)
        self.editor = edits.SessionEdits(lambda:self.repo,self.mappings)
        self.session = app.Session()
        self.queue = SimpleNamespace(repository=self.repo,condition=threading.RLock(),jobs=[],relocate=lambda *a:None)
        self.reviews = SimpleNamespace(lock=threading.RLock(),scanning=set())
        for target,value in [('repository',self.repo),('session_edits',self.editor),('session',self.session),
                             ('analysis_queue',self.queue),('reviews',self.reviews),('discreet',False)]:
            patcher = patch.object(app,target,value);patcher.start();self.addCleanup(patcher.stop)
        self.client = TestClient(app.app)

    def preview(self,operation='mappings',sid=None):
        return self.editor.preview(sid or self.sid,operation)

    def apply(self,operation='mappings',sid=None):
        sid = sid or self.sid
        return self.editor.apply(sid,self.preview(operation,sid)['token'])

    def rename(self,title,sid=None):
        sid = sid or self.sid
        return self.editor.rename(sid,title,self.editor.info(sid)['revision'])

    def tree(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_preview_is_read_only_and_shows_exact_changes(self):
        before = self.tree();preview = self.preview()
        self.assertEqual(self.tree(),before)
        self.assertEqual(preview['changed_blocks'],1)
        self.assertEqual(preview['changes'][0]['after'],'Beta beta Árbol técnico')
        self.assertIn(str(self.mapping),preview['files'][0]['path'])

    def test_rename_preserves_date_paths_ids_and_body_and_reopens(self):
        before = self.repo.get(self.sid)
        txt = Path(before['txt_path']);body = txt.read_bytes()
        after = self.rename('Revisión 👨‍👩‍👧 — sesión / uno')
        self.assertEqual(after['date'],before['date'])
        for key in ('session_id','txt_path','archive_path','history','markers','parts','context'):
            self.assertEqual(after[key],before[key])
        self.assertTrue(txt.read_bytes().endswith(body))
        self.assertTrue(txt.read_text(encoding='utf-8').startswith('# Revisión'))
        reopened = SessionRepository(lambda:self.root,lambda:None,self.root/'other-index.json')
        self.assertEqual(reopened.register(self.path)['title'],after['title'])
        self.assertTrue(list((edits.edit_root(self.path)/'revisions').glob('*/txt.before')))

    def test_title_validation_keeps_existing_files_unchanged(self):
        before = self.tree()
        for title in ('','   ','x'*121,'Título\nsecreto','Título\tincorrecto','Nulo\x00'):
            with self.assertRaises(ValueError): self.rename(title)
        self.assertEqual(self.tree(),before)

    def test_repeat_uses_immutable_base_and_current_rules_without_cascading(self):
        before = self.repo.get(self.sid)
        first = self.apply()
        base = edits.metadata(self.path)['base']
        self.assertEqual(base[before['history'][0]['id']],before['history'][0]['text'])
        self.assertEqual(self.preview()['changes'],[])
        self.mapping.write_text('Beta\tGamma\tword\tnocase\n',encoding='utf-8')
        second = self.apply()
        self.assertEqual(second['history'][0]['text'],'alfa Gamma ÁRBOL')
        self.assertEqual(edits.metadata(self.path)['base'],base)
        self.assertEqual(second['history'][0]['id'],first['history'][0]['id'])
        for key in ('time','start','end','source'): self.assertEqual(before['history'][0][key],second['history'][0][key])

    def test_replacements_never_change_title_context_notes_or_markers(self):
        self.rename('alfa — asunto')
        data=json.loads(self.path.read_text(encoding='utf-8'))
        data['context']='alfa';data['markers']=[{'id':'m','text':'alfa','start':3}]
        note={'id':'n','type':'note','time':'00:00:03','start':3,'text':'alfa'}
        data['history'].append(note);atomic_json(self.path,data)
        txt=Path(self.repo.get(self.sid)['txt_path'])
        with txt.open('a',encoding='utf-8') as file:file.write('\n[00:00:03] — alfa —\n')
        after=self.apply()
        self.assertEqual(after['title'],'alfa — asunto')
        self.assertEqual(after['context'],'alfa');self.assertEqual(after['markers'],data['markers'])
        self.assertEqual(after['history'][-1],note)

    def test_edit_does_not_hold_the_queue_while_waiting_for_capture_finalization(self):
        entered,done=threading.Event(),threading.Event()
        errors=[]
        def edit():
            entered.set()
            try:
                with app.editing_session(self.sid):pass
            except Exception as exc:errors.append(exc)
            done.set()
        self.session.lock.acquire()
        worker=threading.Thread(target=edit)
        try:
            worker.start();self.assertTrue(entered.wait(1));time.sleep(.03)
            acquired=self.queue.condition.acquire(timeout=.2)
            if acquired:self.queue.condition.release()
            self.assertTrue(acquired,'Capture finalization must be able to enqueue its analysis')
        finally:
            self.session.lock.release();worker.join(2)
        self.assertTrue(done.is_set());self.assertEqual(errors,[])

    def test_active_session_and_stale_title_revision_are_rejected(self):
        self.session.session_id=self.sid;self.session.state='recording';self.session.finalized=False
        self.assertEqual(self.client.get(f'/sessions/{self.sid}/editing').status_code,409)
        self.session.session_id=None
        info=self.editor.info(self.sid)
        with Path(self.repo.get(self.sid)['txt_path']).open('a',encoding='utf-8') as file:file.write('\n')
        response=self.client.patch(f'/sessions/{self.sid}/title',json={'title':'Borrador conservado','revision':info['revision']})
        self.assertEqual(response.status_code,409)
        self.assertNotEqual(self.repo.get(self.sid)['title'],'Borrador conservado')

    def test_restore_keeps_later_title_markers_and_notes(self):
        original = self.repo.get(self.sid)
        self.apply();self.rename('Título posterior')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        note = {'id':'note1','type':'note','time':'00:00:03','start':3,'text':'Marcador: Nota nueva','marker_id':'m1'}
        data['history'].append(note);data['markers']=[{'id':'m1','start':3,'text':'Nota nueva'}]
        atomic_json(self.path,data)
        with Path(original['txt_path']).open('a',encoding='utf-8') as file:file.write('\n[00:00:03] — Marcador: Nota nueva —\n')
        restored = self.apply('restore')
        self.assertEqual(restored['history'][0]['text'],original['history'][0]['text'])
        self.assertEqual(restored['title'],'Título posterior')
        self.assertEqual(restored['history'][1],note)
        self.assertEqual(restored['markers'],data['markers'])
        self.assertEqual(restored['parts'],original['parts'])

    def test_legacy_headerless_bom_crlf_and_empty_replacement_keep_stable_ids(self):
        legacy = self.root/'Antigua.txt'
        legacy.write_bytes(b'\xef\xbb\xbf[00:00:01] [Yo] alfa\r\n# comentario\r\n[00:00:02] [Otros] beta\r\n')
        os.utime(legacy,(1600000000,1600000000))
        before = self.repo.register(legacy);sid = before['session_id']
        self.mapping.write_text('alfa\t\tword\tnocase\n',encoding='utf-8')
        corrected = self.apply(sid=sid)
        self.assertEqual(corrected['history'][0]['text'],'')
        renamed = self.rename('Reunión antigua',sid)
        self.assertEqual(renamed['date'],before['date'])
        self.assertEqual([b['id'] for b in renamed['history']],[b['id'] for b in before['history']])
        raw = legacy.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf# Reuni'))
        self.assertIn(b'# comentario\r\n',raw)
        self.assertNotIn(b'\n',raw.replace(b'\r\n',b''))
        restored = self.apply('restore',sid)
        self.assertEqual(restored['title'],'Reunión antigua')
        self.assertEqual(restored['history'],before['history'])
        self.assertEqual(restored['parts'],[])

    def test_legacy_plain_line_can_be_emptied_then_restored(self):
        legacy = self.root/'simple.txt';legacy.write_text('alfa\n',encoding='utf-8')
        sid = self.repo.register(legacy)['session_id']
        self.mapping.write_text('alfa\t\tword\tnocase\n',encoding='utf-8')
        self.assertEqual(self.apply(sid=sid)['history'][0]['text'],'')
        self.rename('Una línea',sid)
        self.assertEqual(self.apply('restore',sid)['history'][0]['text'],'alfa')

    def test_external_session_txt_and_mapping_changes_conflict(self):
        for which in ('manifest','txt','mapping','metadata'):
            preview = self.preview()
            if which=='manifest': target=self.path
            elif which=='txt': target=Path(self.repo.get(self.sid)['txt_path'])
            elif which=='mapping': target=self.mapping
            else:
                self.rename('Nuevo título')
                target=edits.edit_root(self.path)/'metadata.json'
                preview=self.preview()
            raw=target.read_bytes()
            target.write_bytes(raw+(b' ' if which in ('manifest','metadata') else b'\n'))
            with self.assertRaises(MappingConflict):self.editor.apply(self.sid,preview['token'])
            target.write_bytes(raw)

    def test_missing_files_no_rules_and_multiple_mapping_sources(self):
        second=self.root/'second.tsv';second.write_text('beta\tBETA\tword\tcase\n',encoding='utf-8')
        self.files.append(second)
        preview=self.preview();self.assertEqual(len(preview['files']),2)
        second.unlink()
        with self.assertRaises(ValueError):self.preview()
        self.files.pop();self.mapping.write_text('',encoding='utf-8')
        self.assertEqual(self.preview()['changes'],[])
        Path(self.repo.get(self.sid)['txt_path']).unlink()
        with self.assertRaises(OSError):self.preview()

    def test_failed_replace_rolls_back_all_files_and_never_confirms_success(self):
        preview=self.preview();before=self.tree()
        real=edits.atomic_bytes
        failed=[False]
        def fail_manifest(path,content):
            if path==self.path and not failed[0]:
                failed[0]=True;raise OSError('Synthetic manifest failure')
            return real(path,content)
        with patch('session_edits.atomic_bytes',side_effect=fail_manifest):
            with self.assertRaises(OSError):self.editor.apply(self.sid,preview['token'])
        for relative,content in before.items():self.assertEqual((self.root/relative).read_bytes(),content)
        self.assertFalse((edits.edit_root(self.path)/'metadata.json').exists())
        self.assertFalse((edits.edit_root(self.path)/'transaction.json').exists())

    def test_crash_mid_commit_is_recovered_before_session_is_read(self):
        preview=self.preview();before=self.repo.get(self.sid)
        real=edits.atomic_bytes
        def crash(path,content):
            if path.name=='metadata.json':raise KeyboardInterrupt('Synthetic process death')
            return real(path,content)
        with patch('session_edits.atomic_bytes',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.editor.apply(self.sid,preview['token'])
        self.assertTrue((edits.edit_root(self.path)/'transaction.json').exists())
        self.assertEqual(self.repo.get(self.sid)['history'],before['history'])
        self.assertFalse((edits.edit_root(self.path)/'transaction.json').exists())

    def test_recovery_refuses_to_overwrite_a_later_external_change(self):
        preview=self.preview()
        real=edits.atomic_bytes
        def crash(path,content):
            if path.name=='metadata.json':raise KeyboardInterrupt()
            return real(path,content)
        with patch('session_edits.atomic_bytes',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.editor.apply(self.sid,preview['token'])
        txt=Path(self.root/(self.sid+'.txt'));txt.write_text('Edición externa posterior',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'recuperación pendiente'):self.repo.get(self.sid)
        self.assertEqual(txt.read_text(encoding='utf-8'),'Edición externa posterior')

    def test_derived_analysis_stales_without_changing_saved_result_or_calling_api(self):
        previous={'state':'completed','result':{'text':'Resultado anterior'}}
        self.repo.save_analysis(self.sid,previous)
        with patch('analysis.request_completion') as request:self.apply()
        self.assertTrue(self.repo.analysis(self.sid)['stale'])
        self.assertEqual(self.repo.analysis(self.sid)['result'],previous['result'])
        request.assert_not_called()
        current=edits.text_revision(self.repo.get(self.sid))
        self.repo.save_analysis(self.sid,{'state':'completed','source_revision':current,'result':{'source_revision':current,'text':'Nuevo'}})
        self.assertFalse(self.repo.analysis(self.sid)['stale'])
        self.rename('Otro nombre')
        self.assertFalse(self.repo.analysis(self.sid)['stale'])

    def api_rename(self,title,sid=None):
        sid = sid or self.sid
        info = self.client.get(f'/sessions/{sid}/editing').json()
        response = self.client.patch(f'/sessions/{sid}/title',json={'title':title,'revision':info['revision']})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_markdown_session_preserves_editing_analysis_and_associated_files(self):
        manifest = json.loads(self.path.read_text(encoding='utf-8'))
        old = Path(self.repo.get(self.sid)['txt_path'])
        doc = old.with_suffix('.md')
        old.rename(doc)
        manifest['txt'] = '../' + doc.name
        atomic_json(self.path, manifest)
        self.repo.save_analysis(self.sid, self.result())
        self.assertEqual(len(self.repo.list()['sessions']), 1)
        self.assertEqual(self.apply()['history'][0]['text'], 'Beta beta Árbol técnico')
        data = self.api_rename('Markdown Ágil')
        renamed = Path(data['txt_path'])
        self.assertEqual(renamed.suffix, '.md')
        self.assertFalse(renamed.with_suffix('.txt').exists())
        self.assertTrue(renamed.with_suffix('.analysis.md').is_file())
        reopened = SessionRepository(lambda:self.root,lambda:None,self.root/'index.json')
        self.assertEqual(len(reopened.list()['sessions']), 1)
        self.assertEqual(reopened.get(self.sid)['history'], data['history'])
        removed = []
        self.repo.delete(self.sid, lambda paths: removed.extend(paths))
        self.assertIn(renamed, removed)
        self.assertIn(renamed.with_suffix('.analysis.md'), removed)
        self.assertIn(Path(data['archive_path']).parent, removed)

    def test_standalone_markdown_keeps_id_edits_and_analysis_on_rename(self):
        doc = self.root/'Antigua.md'
        doc.write_text('# Antigua\n\n[00:00:01] [Otros] alfa beta ÁRBOL\n',encoding='utf-8')
        sid = self.repo.register(doc)['session_id']
        self.repo.save_analysis(sid,self.result())
        data = self.api_rename('Sesión Markdown',sid)
        renamed = Path(data['txt_path'])
        self.assertEqual(renamed.suffix,'.md')
        self.assertEqual(data['session_id'],sid)
        self.assertTrue(renamed.with_suffix('.md.edits').is_dir())
        self.assertTrue(renamed.with_suffix('.analysis.json').is_file())
        reopened = SessionRepository(lambda:self.root,lambda:None,self.root/'index.json')
        reopened.list()
        self.assertEqual(reopened.get(sid)['session_id'],sid)
        self.assertEqual(reopened.get(sid)['history'],data['history'])
        self.assertIn(renamed.with_suffix('.md.edits'),self.repo.files(sid))
        with self.assertRaises(ValueError):
            self.repo.register(renamed.with_suffix('.analysis.md'))

    def test_rename_moves_transcript_and_folder_keeping_the_timestamp(self):
        data = self.api_rename('Daily: siigo/x')
        self.assertEqual(data['session_id'],self.sid)
        self.assertEqual(Path(data['txt_path']).name,'2026-10-28_120000-Daily siigox.txt')
        self.assertTrue((self.root/'2026-10-28_120000-Daily siigox.session'/'session.json').is_file())
        self.assertFalse(self.path.exists() or (self.root/('a'*32+'.txt')).exists())
        self.assertEqual(data['history'][0]['text'],'alfa beta ÁRBOL')
        again = self.api_rename('Daily producto')  # the timestamp comes from the existing name
        self.assertEqual(Path(again['txt_path']).name,'2026-10-28_120000-Daily producto.txt')
        self.assertEqual(self.apply()['history'][0]['text'],'Beta beta Árbol técnico')  # edits still work

    def test_rename_avoids_name_collisions(self):
        (self.root/'2026-10-28_120000-Repetida.txt').write_text('otra',encoding='utf-8')
        data = self.api_rename('Repetida')
        self.assertEqual(Path(data['txt_path']).name,'2026-10-28_120000-Repetida (1).txt')

    def test_legacy_txt_rename_keeps_id_analysis_and_edits(self):
        txt = self.root/'ago-06-conociendo.txt'
        txt.write_text('# Conociendo\n\n[00:00:01] [Otros] Hola\n',encoding='utf-8')
        sid = self.repo.register(txt)['session_id']
        self.repo.save_analysis(sid,{'state':'completed'})
        data = self.api_rename('Conociendo Siigo X',sid)
        self.assertEqual(data['session_id'],sid)
        new = Path(data['txt_path'])
        self.assertRegex(new.name,r'^\d{4}-\d\d-\d\d_\d{6}-Conociendo Siigo X\.txt$')
        self.assertTrue(new.with_suffix('.analysis.json').is_file())
        self.assertTrue(new.with_suffix('.txt.edits').is_dir())
        reopened = SessionRepository(lambda:self.root,lambda:None,self.root/'index.json')
        self.assertIn(sid,[r['session_id'] for r in reopened.list()['sessions']])

    def result(self,stamp='2026-10-28T13:00:00'):
        return {'state':'completed','result':{'text':'## Compromisos\n- Enviar informe [00:01:05] y revisar [0:02:10]','model':'luna','created_at':stamp}}

    def test_analysis_document_is_clean_markdown_with_insights(self):
        manifest = json.loads(self.path.read_text(encoding='utf-8'))
        manifest['insights'] = [{'id':'a','type':'commitment','text':'Le escribo mañana','start':3,'pinned':True,'dismissed':False},
                                {'id':'b','type':'risk','text':'Ruido','start':4,'pinned':False,'dismissed':True}]
        self.path.write_text(json.dumps(manifest),encoding='utf-8')
        self.repo.save_analysis(self.sid,self.result())
        doc = self.repo.markdown_path(self.sid)
        self.assertEqual(doc.parent,self.root)
        self.assertTrue(doc.name.endswith('.analysis.md'))
        text = doc.read_text(encoding='utf-8')
        self.assertIn('- Enviar informe y revisar',text)
        self.assertNotRegex(text,r'\[\d+:\d\d:\d\d\]')
        self.assertIn('**Compromiso** 📌: Le escribo mañana',text)
        self.assertNotIn('Ruido',text)
        self.assertEqual(self.client.get(f'/sessions/{self.sid}/analysis/markdown').json()['text'],text)

    def test_user_edits_to_the_document_survive_until_a_new_result(self):
        self.repo.save_analysis(self.sid,self.result())
        doc = self.repo.markdown_path(self.sid)
        doc.write_text('mis notas',encoding='utf-8')
        self.repo.save_analysis(self.sid,{**self.result(),'state':'running'})  # progress save, same result
        self.assertEqual(doc.read_text(encoding='utf-8'),'mis notas')
        self.repo.save_analysis(self.sid,self.result('2026-10-29T09:00:00'))
        self.assertNotEqual(doc.read_text(encoding='utf-8'),'mis notas')

    def test_rename_and_delete_include_the_analysis_document(self):
        self.repo.save_analysis(self.sid,self.result())
        data = self.api_rename('Con documento')
        doc = Path(data['txt_path']).with_suffix('.analysis.md')
        self.assertTrue(doc.is_file())
        self.assertIn(doc,self.repo.files(self.sid))

    def test_missing_analysis_has_no_document(self):
        self.assertEqual(self.client.get(f'/sessions/{self.sid}/analysis/markdown').status_code,404)

    def test_locked_files_keep_title_change_and_original_names(self):
        original = set(p.name for p in self.root.iterdir())
        with patch('pathlib.Path.rename',side_effect=PermissionError('en uso')):
            data = self.api_rename('Bloqueada')
        self.assertIn('no se pudieron renombrar',data['files_warning'])
        self.assertEqual(data['title'],'Bloqueada')
        self.assertEqual(set(p.name for p in self.root.iterdir()),original)

    def test_api_freezes_session_and_checks_busy_privacy_and_revision(self):
        sid=self.sid
        preview_url=f'/sessions/{sid}/text-preview'
        self.queue.jobs=[{'session_id':sid,'state':'pending'}]
        self.assertEqual(self.client.post(preview_url,json={}).status_code,409)
        info=self.client.get(f'/sessions/{sid}/editing').json()
        self.assertEqual(self.client.patch(f'/sessions/{sid}/title',json={'title':'Renombrada','revision':info['revision']}).status_code,200)
        self.queue.jobs=[];self.reviews.scanning.add(sid)
        self.assertEqual(self.client.post(preview_url,json={}).status_code,409)
        self.reviews.scanning.clear()
        preview=self.client.post(preview_url,json={}).json()
        self.assertEqual(self.client.post(f'/sessions/{sid}/text-apply',json={'token':preview['token']}).status_code,200)
        with patch.object(app,'discreet',True):
            self.assertEqual(self.client.get(f'/sessions/{sid}/editing').status_code,409)

    def test_finished_controller_is_updated_and_another_capture_is_untouched(self):
        data=self.repo.get(self.sid)
        self.session.session_id=self.sid;self.session.finalized=True;self.session.state='finished'
        self.session.history=copy.deepcopy(data['history']);self.session.title=data['title']
        self.repo.current=lambda:{'session_id':self.sid,'state':'finished','history':[{'text':'obsolete'}],'title':'Old'}
        preview=self.client.post(f'/sessions/{self.sid}/text-preview',json={}).json()
        response=self.client.post(f'/sessions/{self.sid}/text-apply',json={'token':preview['token']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.session.history,response.json()['history'])
        self.assertNotEqual(self.session.history[0]['text'],'obsolete')
        self.session.session_id='other';self.session.state='recording'
        history=copy.deepcopy(self.session.history)
        info=self.client.get(f'/sessions/{self.sid}/editing').json()
        response=self.client.patch(f'/sessions/{self.sid}/title',json={'title':'Reunión terminada','revision':info['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.session.state,'recording');self.assertEqual(self.session.history,history)


if __name__=='__main__':unittest.main()
