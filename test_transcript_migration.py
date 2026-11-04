"""Migration uses only disposable sessions and never inspects real processes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recording import atomic_json
from session_edits import SessionEdits, edit_root
from session_repository import SessionRepository
from mapping_store import MappingStore
from test_analysis import make_session
from tools import convert_transcripts_to_md as migration


class TranscriptMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root/'transcripciones'
        self.folder.mkdir()
        self.state = self.root/'state'
        self.state.mkdir()
        self.repo = self.repository()

    def repository(self):
        return SessionRepository(lambda:self.folder,lambda:None,self.state/'session-index.json')

    def plan(self):
        return migration.build_plan(self.folder,self.state)

    def apply(self,plan=None):
        return migration.apply_plan(plan or self.plan(),running_check=lambda:False)

    def legacy(self,name='Reunión Ágil.txt'):
        path = self.folder/name
        path.write_bytes('# Reunión Ágil\r\n\r\n[00:00:01] [Otros] alfa 😀\r\n'.encode('utf-8-sig'))
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and p.name != 'transcript-md-migration.json'}

    def test_preview_does_not_write_and_preserves_legacy_identity_and_content(self):
        txt = self.legacy()
        data = self.repo.register(txt)
        original = self.snapshot()
        plan = self.plan()
        self.assertEqual(self.snapshot(),original)
        self.assertEqual(self.apply(plan),1)
        target = txt.with_suffix('.md')
        self.assertFalse(txt.exists())
        self.assertEqual(target.read_bytes(),next(iter(original.values())))
        repo = self.repository()
        self.assertEqual(len(repo.list()['sessions']),1)
        after = repo.get(data['session_id'])
        for key in ('session_id','title','history','date'):
            self.assertEqual(after[key],data[key])
        self.assertEqual(migration.recover(self.folder,self.state,lambda:False),
                         'La conversión ya terminó; se conserva el registro como evidencia')
        with self.assertRaises(RuntimeError):
            self.plan()
        self.assertEqual(len(repo.list()['sessions']),1)

    def test_structured_session_keeps_audio_analysis_and_edits(self):
        path = make_session(self.folder,text='alfa 😀')
        data = self.repo.register(path)
        sid = data['session_id']
        editor = SessionEdits(lambda:self.repo,MappingStore(lambda:[]))
        editor.rename(sid,'Título editado',editor.info(sid)['revision'])
        data = self.repo.get(sid)
        audio = path.parent/'part-000.wav'
        audio.write_bytes(b'preservar audio')
        self.repo.save_analysis(sid,{'state':'completed','source_path':str(path),
                                   'result':{'text':'Síntesis','created_at':'2026-11-03'}})
        preserved = {p:p.read_bytes() for p in path.parent.rglob('*') if p.is_file() and p != path}
        analysis_md = Path(data['txt_path']).with_suffix('.analysis.md')
        analysis_content = analysis_md.read_bytes()
        self.assertEqual(self.apply(),1)
        repo = self.repository()
        self.assertEqual(len(repo.list()['sessions']),1)
        after = repo.get(sid)
        self.assertEqual(after['history'],data['history'])
        self.assertEqual(Path(after['txt_path']).suffix,'.md')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['txt'],
                         '../' + Path(after['txt_path']).name)
        for p,content in preserved.items():
            self.assertEqual(p.read_bytes(),content)
        self.assertEqual(analysis_md.read_bytes(),analysis_content)
        editor = SessionEdits(lambda:repo,MappingStore(lambda:[]))
        self.assertEqual(editor.rename(sid,'Otro título',editor.info(sid)['revision'])['title'],'Otro título')

    def test_external_index_jobs_analysis_and_legacy_revision_tree_are_preserved(self):
        external = self.root/'externas';external.mkdir()
        txt = external/'externa.txt'
        txt.write_text('# Externa\n[00:00:01] [Otros] Hola',encoding='utf-8')
        data = self.repo.register(txt,external=True)
        editor = SessionEdits(lambda:self.repo,MappingStore(lambda:[]))
        editor.rename(data['session_id'],'Externa editada',editor.info(data['session_id'])['revision'])
        before = self.repo.get(data['session_id'])
        revisions = {str(p.relative_to(edit_root(txt))):p.read_bytes()
                     for p in (edit_root(txt)/'revisions').rglob('*') if p.is_file()}
        atomic_json(self.state/'analysis-queue.json',[{'session_id':data['session_id'],
                    'state':'completed','source_path':str(txt)}])
        self.repo.save_analysis(data['session_id'],{'state':'completed','source_path':str(txt)})
        self.apply()
        target = txt.with_suffix('.md')
        self.assertEqual(migration.read_json(self.state/'session-index.json',[]),[str(target)])
        self.assertEqual(migration.read_json(self.state/'analysis-queue.json',[])[0]['source_path'],str(target))
        self.assertEqual(migration.read_json(txt.with_suffix('.analysis.json'),{})['source_path'],str(target))
        for name,content in revisions.items():
            self.assertEqual((edit_root(target)/name).read_bytes(),content)
        repo = self.repository();repo.list()
        self.assertEqual(repo.get(data['session_id'])['history'],before['history'])

    def test_collision_stops_entire_preview_without_writes(self):
        self.legacy('primera.txt')
        txt = self.legacy('segunda.txt')
        txt.with_suffix('.md').write_bytes(b'no sobrescribir')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(self.snapshot(),before)

    def test_busy_app_or_pending_jobs_block_apply(self):
        self.legacy()
        plan = self.plan()
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            migration.apply_plan(plan,lambda:True)
        self.assertEqual(self.snapshot(),before)
        for status in ('pending','running'):
            atomic_json(self.state/'analysis-queue.json',[{'state':status}])
            with self.assertRaises(RuntimeError):
                self.apply(plan)

    def test_source_change_after_preview_is_preserved(self):
        txt = self.legacy()
        plan = self.plan()
        txt.write_bytes(b'cambio externo')
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertEqual(txt.read_bytes(),b'cambio externo')
        self.assertFalse(txt.with_suffix('.md').exists())

    def test_failure_rolls_back_the_entire_batch(self):
        self.legacy('primera.txt')
        second = self.legacy('segunda.txt')
        before = self.snapshot()
        rename = Path.rename
        def fail(path,target):
            if path == second:
                raise PermissionError('archivo bloqueado')
            return rename(path,target)
        with patch.object(Path,'rename',fail):
            with self.assertRaises(PermissionError):
                self.apply()
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(migration.read_json(self.state/'transcript-md-migration.json',{})['status'],'rolled_back')

    def test_interruption_can_be_recovered_after_restart(self):
        self.legacy()
        before = self.snapshot()
        write = migration.atomic_json
        def interrupt(path,data):
            if data.get('status') == 'committed':
                raise KeyboardInterrupt()
            return write(path,data)
        with patch.object(migration,'atomic_json',interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        migration.recover(self.folder,self.state,lambda:False)
        self.assertEqual(self.snapshot(),before)

    def test_recovery_preserves_external_changes_for_manual_review(self):
        txt = self.legacy()
        write = migration.atomic_json
        def interrupt(path,data):
            if data.get('status') == 'committed':
                raise KeyboardInterrupt()
            return write(path,data)
        with patch.object(migration,'atomic_json',interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        target = txt.with_suffix('.md')
        target.write_bytes(b'cambio externo')
        with self.assertRaises(ValueError):
            migration.recover(self.folder,self.state,lambda:False)
        self.assertEqual(target.read_bytes(),b'cambio externo')

    def test_pending_edit_transaction_blocks_preview_without_recovery(self):
        txt = self.legacy()
        edit_root(txt).mkdir()
        (edit_root(txt)/'transaction.json').write_text('{}',encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(self.snapshot(),before)

    def test_old_manifest_without_date_keeps_its_display_date_and_modification_time(self):
        path = make_session(self.folder)
        data = migration.read_json(path,{})
        data.pop('created_at')
        data['version'] = 1
        atomic_json(path,data)
        os.utime(path,ns=(1_700_000_000_000_000_000,1_700_000_001_123_456_700))
        timestamp = path.stat().st_mtime_ns
        before = self.repo.register(path)
        self.apply()
        repo = self.repository();repo.list()
        self.assertEqual(repo.get(before['session_id'])['date'],before['date'])
        self.assertEqual(path.stat().st_mtime_ns,timestamp)

    def test_rollback_restores_manifest_modification_time(self):
        path = make_session(self.folder)
        os.utime(path,ns=(1_700_000_000_000_000_000,1_700_000_001_123_456_700))
        timestamp = path.stat().st_mtime_ns
        before = self.snapshot()
        write = migration.atomic_json
        def interrupt(path,data):
            if data.get('status') == 'committed':
                raise KeyboardInterrupt()
            return write(path,data)
        with patch.object(migration,'atomic_json',interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        migration.recover(self.folder,self.state,lambda:False)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(path.stat().st_mtime_ns,timestamp)


if __name__ == '__main__':
    unittest.main()
