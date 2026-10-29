"""Dictionary and TSV regression tests, with isolated user files only."""
import codecs
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from corrections import Corrections
from mapping_store import MappingStore, MappingConflict, read_document
from vocabulary import Vocabulary


class MappingStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'mapping.tsv'
        self.original=codecs.BOM_UTF8+b'# comment\r\n\r\nsi go\tSiigo\tword\tnocase\r\nmalformed\r\nJose\tJos\xc3\xa9\tword\tcase'
        self.path.write_bytes(self.original)
        self.store=MappingStore(lambda:[self.path])

    def mutate(self, operation, rule=None, row_id=None):
        info=self.store.snapshot()['files'][0]
        return self.store.mutate(info['id'],info['revision'],operation,row_id,rule)

    def test_preserves_bom_comments_malformed_lines_and_line_endings(self):
        before=self.store.snapshot();self.assertEqual(before['warnings'][0]['line'],4)
        result=self.mutate('edit',('si go','Siigo X','word','nocase'),'3')
        self.assertEqual(self.path.read_bytes(),self.original.replace(b'\tSiigo\t',b'\tSiigo X\t'))
        self.assertEqual(Path(result['backup']).read_bytes(),self.original)
        self.mutate('add',('se ge si','SGSI','word','nocase'))
        self.assertTrue(self.path.read_bytes().endswith(b'\r\nse ge si\tSGSI\tword\tnocase\r\n'))
        self.mutate('delete',row_id='3')
        self.assertIn(b'malformed\r\n',self.path.read_bytes())

    def test_external_change_conflict_preserves_external_content(self):
        info=self.store.snapshot()['files'][0]
        changed=self.original+b'\r\n# external'
        self.path.write_bytes(changed)
        with self.assertRaises(MappingConflict):
            self.store.mutate(info['id'],info['revision'],'add',rule=('new','nuevo','word','nocase'))
        self.assertEqual(self.path.read_bytes(),changed)
        self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_external_change_during_save_is_not_overwritten(self):
        import shutil
        original_copy=shutil.copy2
        changed=b'# changed by other application\n'
        def concurrent_copy(source,target):
            original_copy(source,target)
            self.path.write_bytes(changed)
        with patch('mapping_store.shutil.copy2',side_effect=concurrent_copy):
            with self.assertRaises(MappingConflict):self.mutate('add',('new','nuevo','word','nocase'))
        self.assertEqual(self.path.read_bytes(),changed)

    def test_storage_errors_do_not_confirm_or_replace_original(self):
        with patch('mapping_store.shutil.copy2',side_effect=PermissionError('denied')):
            with self.assertRaises(OSError):self.mutate('add',('new','nuevo','word','nocase'))
        self.assertEqual(self.path.read_bytes(),self.original)
        with patch('mapping_store.os.replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):self.mutate('add',('new','nuevo','word','nocase'))
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_duplicate_validation_uses_all_effective_files(self):
        second=self.root/'second.tsv';second.write_text('Hola\tHello\tword\tnocase\n',encoding='utf-8')
        self.store=MappingStore(lambda:[self.path,second])
        with self.assertRaises(ValueError):self.mutate('add',('hola','Otra','word','nocase'))
        self.mutate('add',('hola','Otra','word','case'))
        self.assertEqual(len(self.store.snapshot()['files']),2)

    def test_invalid_fields_and_protected_terms(self):
        for rule in [('', 'x','word','case'),('x\ty','a','word','case'),('x','a\nb','word','case'),('#x','y','word','case')]:
            with self.assertRaises(ValueError):self.mutate('add',rule)
        store=MappingStore(lambda:[self.path],protected_roots=(self.root,))
        info=store.snapshot()['files'][0];self.assertFalse(info['editable'])
        with self.assertRaises(ValueError):store.mutate(info['id'],info['revision'],'delete','3')
        with self.assertRaises(KeyError):self.store.resolve('../mapping.tsv')

    def test_engine_snapshot_and_supported_matching_behaviors(self):
        self.path.write_text('si go\tSiigo\tword\tnocase\nJose\tJosé\tword\tcase\npre\tX\tany\tnocase\nruido\t\tword\tnocase\nsi go x\tSiigo X\tword\tnocase\n',encoding='utf-8')
        engine=Corrections(str(self.path));frozen=engine.snapshot()
        self.assertEqual(engine.apply('SI GO y si go x Jose jose siempre ruido'), 'Siigo y Siigo X José jose siemX ')
        self.path.write_text('si go\tNueva\tword\tnocase\n',encoding='utf-8');engine.reload_if_changed()
        self.assertEqual(engine.apply('si go'),'Nueva');self.assertEqual(frozen.apply('si go'),'Siigo')
        self.assertIn('Siigo',frozen.hotwords())

    def test_two_columns_default_flags_and_empty_replacement(self):
        self.path.write_text('hola\tHello\nadiós\t\t\t\n',encoding='utf-8')
        self.assertEqual(Corrections(str(self.path)).apply('HOLA adiós holanda'),'Hello  holanda')


class VocabularyTest(unittest.TestCase):
    def test_offline_terms_flags_pagination_and_empty_own_glossary(self):
        vocabulary=Vocabulary(app.BASE/'dictionaries')
        spanish=vocabulary.search('es','Colombia')
        self.assertIn('Colombia',[x['term'] for x in spanish['entries']])
        english=vocabulary.search('en','computer')
        self.assertTrue(any(x['term']=='computer' for x in english['entries']))
        self.assertTrue(all('/' not in x['term'] for x in vocabulary.search('es',page_size=100)['entries']))
        self.assertNotEqual(vocabulary.search('en',page=1)['entries'],vocabulary.search('en',page=2)['entries'])
        self.assertEqual(vocabulary.search('own')['source'],'own')

    def test_own_glossary_is_separate_and_supports_both_languages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'sources.json').write_text(json.dumps({'repository':'fixture','commit':'a'*40,'languages':{}}),encoding='utf-8')
            for language in ('es','en'):
                folder=root/language;folder.mkdir()
                (folder/'index.aff').write_text('SET UTF-8\n',encoding='utf-8')
                (folder/'index.dic').write_text('1\nPrueba/AB\n',encoding='utf-8')
            (root/'terms.tsv').write_text('# comentario\nSGSI\tboth\tSeguridad de la información\n',encoding='utf-8')
            entries=Vocabulary(root).search('own','sgsi')['entries']
            self.assertEqual(entries,[{'term':'SGSI','language':'both','source':'own','note':'Seguridad de la información'}])

    def test_vendored_sources_have_matching_checksums(self):
        root=app.BASE/'dictionaries';manifest=json.loads((root/'sources.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['commit']),40)
        for language,source in manifest['languages'].items():
            for name,info in source['files'].items():
                self.assertEqual(hashlib.sha256((root/language/name).read_bytes()).hexdigest(),info['sha256'])


class DictionaryApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=self.root/'mappings.tsv';self.path.write_text('si go\tSiigo\tword\tnocase\n',encoding='utf-8')
        for target,value in [('settings.PATH',self.root/'settings.json'),('app.ARGS.dict',None),
                             ('app.fixes',Corrections(str(self.path)))]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'TRANSCRIPTOR_DICT':'','LOCALAPPDATA':str(self.root/'local')});p.start();self.addCleanup(p.stop)
        app.settings.save({'dict_paths':str(self.path)})
        self.client=TestClient(app.app)

    def test_crud_preview_revision_and_no_arbitrary_paths(self):
        data=self.client.get('/dictionary/mappings').json();file=data['files'][0]
        body={'revision':file['revision'],'source':'se ge si','target':'SGSI','mode':'word','case':'nocase'}
        response=self.client.post(f"/dictionary/mappings/{file['id']}/rules",json=body)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/dictionary/mappings/preview',json={'text':'SI GO y se ge si'}).json()['text'],'Siigo y SGSI')
        self.assertEqual(self.client.post(f"/dictionary/mappings/{file['id']}/rules",json=body).status_code,409)
        self.assertEqual(self.client.post('/dictionary/mappings/not-configured/rules',json=body).status_code,404)
        self.assertEqual(self.client.post('/dictionary/terms',json={'term':'test'}).status_code,405)

    def test_precedence_and_create_personal_are_explicit(self):
        missing=self.root/'missing.tsv';app.settings.save({'dict_paths':str(missing)})
        self.assertTrue(self.client.get('/dictionary/mappings').json()['can_create_personal'])
        response=self.client.post('/dictionary/mappings/create-personal',json={})
        self.assertEqual(response.status_code,200)
        personal=self.root/'local'/'Transcriptor'/'mappings.tsv';self.assertTrue(personal.is_file())
        with patch.object(app.ARGS,'dict',str(missing)):
            self.assertEqual(self.client.get('/dictionary/mappings').json()['configuration_origin'],'--dict')
            self.assertEqual(self.client.post('/dictionary/mappings/create-personal',json={}).status_code,409)
            self.client.post('/settings',json={'dict_paths':str(personal)})
            self.assertEqual(app.current_corrections().paths,[missing])
        with patch.dict(os.environ,{'TRANSCRIPTOR_DICT':str(missing)}):
            self.assertEqual(self.client.get('/dictionary/mappings').json()['configuration_origin'],'TRANSCRIPTOR_DICT')


if __name__=='__main__':unittest.main()
