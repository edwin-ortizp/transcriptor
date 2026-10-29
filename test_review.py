"""Local vocabulary review using temporary sessions and no external API."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from mapping_store import MappingConflict, MappingStore
from personal_terms import PersonalTerms, normalized
from recording import atomic_json
from review import LocalSpelling, ReviewService, find_candidates
from session_repository import SessionRepository
from test_analysis import make_session


def source(text):
    return {'history':[{'id':'block','type':'text','text':text,'start':3,'time':'00:00:03','source':'Otros'}]}


class UnknownWords:
    def __init__(self): self.calls=[]
    def lookup(self,word): self.calls.append(word); return False


class PersonalTermsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'user'/'terms.tsv';self.store=PersonalTerms(lambda:self.path)

    def test_create_unicode_duplicates_and_revision(self):
        self.assertEqual(self.store.read()['revision'],'missing');self.assertFalse(self.path.exists())
        saved=self.store.add('  Cafe\u0301  ','missing')
        self.assertEqual(saved['entries'][0]['term'],'Café')
        with self.assertRaises(ValueError):self.store.add('CAFÉ',saved['revision'])
        with self.assertRaises(MappingConflict):self.store.add('Otra','missing')
        self.assertEqual(len(self.store.read()['entries']),1)

    def test_backup_delete_and_conflicts_preserve_file(self):
        first=self.store.add('QzzName','missing');before=self.path.read_bytes()
        second=self.store.add('QzzOther',first['revision'])
        self.assertEqual(Path(second['backup']).read_bytes(),before)
        external=self.path.read_bytes()+b'# external\n';self.path.write_bytes(external)
        with self.assertRaises(MappingConflict):self.store.delete(first['entries'][0]['id'],second['revision'])
        self.assertEqual(self.path.read_bytes(),external)
        current=self.store.read();deleted=self.store.delete(first['entries'][0]['id'],current['revision'])
        self.assertEqual([e['term'] for e in deleted['entries']],['QzzOther'])
        self.assertIn(b'# external',self.path.read_bytes())

    def test_invalid_fields_and_failed_creation(self):
        for term in ('',' ','x\ty','x\ny','# comment'):
            with self.assertRaises(ValueError):self.store.add(term,'missing')
        with patch('personal_terms.os.link',side_effect=PermissionError('denied')):
            with self.assertRaises(OSError):self.store.add('QzzName','missing')
        self.assertFalse(self.path.exists());self.assertFalse(list(self.path.parent.glob('*.tmp')))


class DetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spelling=LocalSpelling(app.BASE/'dictionaries')
        cls.dictionaries,cls.own=cls.spelling.load()

    def test_real_hunspell_forms_and_bilingual_text(self):
        rows=find_candidates(source('hablábamos trabajadores reuniones computers creating Colombia xqzblorf'),self.dictionaries,self.own,{'entries':[]},[])
        self.assertEqual([r['key'] for r in rows],['xqzblorf'])

    def test_unicode_case_context_and_cache(self):
        dictionary=UnknownWords()
        rows=find_candidates(source('Cafe\u0301 CAFÉ café'),(dictionary,),(),{'entries':[]},[])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['count'],3)
        self.assertEqual(rows[0]['key'],'café');self.assertEqual(rows[0]['occurrences'][0]['start'],3)
        self.assertLessEqual(len(dictionary.calls),2)

    def test_urls_emails_numbers_and_technical_tokens(self):
        rows=find_candidates(source("https://qzz.example/foo ftp://qzz.example/file qzz@example.com qzz'word@example.com 123 2026-10 GPT-4 D'Qzz"),(UnknownWords(),),(),{'entries':[]},[])
        self.assertEqual({r['term'] for r in rows},{'GPT-4',"D'Qzz"})

    def test_mappings_do_not_accept_phrase_components_elsewhere(self):
        mapping={'source':'Qzz Xar','target':'QzzCorrect','mode':'word','case':'nocase'}
        rows=find_candidates(source('Qzz Xar Qzz QZZCORRECT'),(UnknownWords(),),(),{'entries':[]},[mapping])
        self.assertEqual([(r['key'],r['count']) for r in rows],[('qzz',1)])
        mapping={'source':'Qzz','target':'','mode':'word','case':'case'}
        rows=find_candidates(source('Qzz qzz'),(UnknownWords(),),(),{'entries':[]},[mapping])
        self.assertEqual(rows[0]['variants'],{'qzz':1})
        mapping={'source':'Qzz','target':'','mode':'any','case':'nocase'}
        rows=find_candidates(source('Qzz QzzTail'),(UnknownWords(),),(),{'entries':[]},[mapping])
        self.assertEqual([r['term'] for r in rows],['QzzTail'])

    def test_personal_and_own_terms_are_full_terms(self):
        rows=find_candidates(source('Qzz Xar Qzz MyTerm OwnSigla'),(UnknownWords(),),('OwnSigla',),
                             {'entries':[{'term':'Qzz Xar'},{'term':'MyTerm'}]},[])
        self.assertEqual([r['term'] for r in rows],['Qzz'])


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.spelling=LocalSpelling(app.BASE/'dictionaries')
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=make_session(self.root,text='xqzblorf xqzblorf zqword')
        self.second=make_session(self.root,sid='b'*32,text='xqzblorf zqword')
        self.repo=SessionRepository(lambda:self.root,lambda:None,self.root/'index.json')
        self.sid=self.repo.register(self.path)['session_id'];self.other=self.repo.register(self.second)['session_id']
        self.personal=PersonalTerms(lambda:self.root/'personal'/'terms.tsv')
        self.mapping=self.root/'mappings.tsv';self.mapping.write_text('# mappings\n',encoding='utf-8')
        self.mappings=MappingStore(lambda:[self.mapping]);self.events=[]
        self.service=ReviewService(lambda:self.repo,self.spelling,self.personal,self.mappings,self.events.append)
        self.addCleanup(self.join)
    def join(self):
        for thread in self.service.threads.values():thread.join(5)
    def scan(self,sid=None):
        sid=sid or self.sid;self.service.start(sid);self.service.threads[sid].join(10)
        result=self.service.get(sid);self.assertEqual(result['state'],'completed',result);return result
    def row(self,result,key):return next(r for r in result['rows'] if r['key']==key)

    def test_manual_only_and_ignore_is_session_local_and_reversible(self):
        self.assertEqual(self.service.get(self.sid)['state'],'idle');self.assertFalse(self.service.path(self.sid).exists())
        result=self.scan();row=self.row(result,'xqzblorf')
        self.service.decide(self.sid,row['id'],'ignore',result['revision'])
        result=self.scan();self.assertEqual(result['pending'],1)
        other=self.scan(self.other);self.assertEqual(other['pending'],2)
        reviewed=self.service.get(self.sid,status='reviewed');self.assertEqual(reviewed['rows'][0]['status'],'ignored')
        self.service.decide(self.sid,row['id'],'restore',reviewed['revision']);self.assertEqual(self.service.get(self.sid)['pending'],2)

    def test_acceptance_is_personal_persistent_and_does_not_modify_transcription(self):
        manifest,txt=self.path.read_bytes(),(self.root/(self.sid+'.txt')).read_bytes()
        result=self.scan();row=self.row(result,'xqzblorf')
        self.service.decide(self.sid,row['id'],'accept',result['revision'])
        self.assertEqual(self.service.get(self.sid)['pending'],1)
        self.assertEqual(self.scan(self.other)['pending'],1)
        self.assertEqual(self.path.read_bytes(),manifest);self.assertEqual((self.root/(self.sid+'.txt')).read_bytes(),txt)
        restored=ReviewService(lambda:self.repo,self.spelling,self.personal,self.mappings,lambda _:None)
        self.assertEqual(restored.get(self.sid,status='reviewed')['rows'][0]['status'],'accepted')
        personal=self.personal.read();self.personal.delete(personal['entries'][0]['id'],personal['revision'])
        self.assertTrue(self.service.get(self.sid)['stale']);self.assertEqual(self.scan()['pending'],2)

    def test_mapping_requires_coverage_and_reopens_if_removed(self):
        result=self.scan();row=self.row(result,'xqzblorf')
        with self.assertRaises(ValueError):self.service.decide(self.sid,row['id'],'mapping',result['revision'])
        self.mapping.write_text('xqzblorf\tCorrecto\tword\tnocase\n',encoding='utf-8')
        self.assertTrue(self.service.get(self.sid)['stale'])
        self.service.decide(self.sid,row['id'],'mapping',result['revision'])
        result=self.scan();self.assertEqual(result['pending'],1)
        self.mapping.write_text('# removed\n',encoding='utf-8');self.assertEqual(self.scan()['pending'],2)

    def test_failed_personal_write_does_not_record_acceptance(self):
        result=self.scan();row=self.row(result,'xqzblorf')
        with patch.object(self.personal,'add',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.service.decide(self.sid,row['id'],'accept',result['revision'])
        self.assertEqual(self.service.get(self.sid)['pending'],2)

    def test_failure_keeps_previous_results_and_stale_text_is_reported(self):
        result=self.scan();count=result['pending']
        with patch.object(self.spelling,'load',side_effect=ValueError('bad dictionary')):
            self.service.start(self.sid);self.service.threads[self.sid].join(5)
        failed=self.service.get(self.sid);self.assertEqual(failed['state'],'error');self.assertEqual(failed['pending'],count)
        data=json.loads(self.path.read_text(encoding='utf-8'));data['history'][0]['text']='Newqzz'
        atomic_json(self.path,data);self.assertTrue(self.service.get(self.sid)['stale'])

    def test_unwritable_review_reports_error_without_losing_previous_rows(self):
        previous=self.scan()
        with patch('review.atomic_json',side_effect=PermissionError('denied')):
            self.service.start(self.sid);self.service.threads[self.sid].join(5)
        result=self.service.get(self.sid)
        self.assertEqual(result['state'],'error');self.assertTrue(result['error'])
        self.assertEqual(result['pending'],previous['pending'])

    def test_corrupt_review_is_not_overwritten(self):
        self.scan()
        path=self.service.path(self.sid)
        data=json.loads(path.read_text(encoding='utf-8'))
        data['rows'][0]['occurrences'][0]['start']=-1
        atomic_json(path,data);original=path.read_bytes()
        with self.assertRaises(ValueError):self.service.get(self.sid)
        with self.assertRaises(ValueError):self.service.start(self.sid)
        self.assertEqual(path.read_bytes(),original)

    def test_legacy_txt_scan_and_revision_conflicts(self):
        old=self.root/'Antigua.txt';old.write_text('# Antigua\n[00:01:02] [Yo] xqzblorf',encoding='utf-8');before=old.read_bytes()
        sid=self.repo.register(old)['session_id'];result=self.scan(sid)
        self.assertEqual(result['rows'][0]['occurrences'][0]['start'],62)
        self.assertEqual(self.service.path(sid),old.with_suffix('.review.json'));self.assertEqual(before,old.read_bytes())
        with self.assertRaises(MappingConflict):self.service.decide(sid,result['rows'][0]['id'],'ignore','old-revision')

    def test_concurrent_search_is_refused_and_captured_session_is_untouched(self):
        entered,release=threading.Event(),threading.Event();real_load=self.spelling.load
        def slow():entered.set();release.wait(5);return real_load()
        with patch.object(self.spelling,'load',side_effect=slow):
            self.service.start(self.sid);self.assertTrue(entered.wait(2))
            with self.assertRaises(MappingConflict):self.service.start(self.sid)
            release.set();self.service.threads[self.sid].join(5)


class ReviewApiTest(unittest.TestCase):
    def test_no_automatic_search_and_api_uses_temporary_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=make_session(root,text='xqzblorf')
            repository=SessionRepository(lambda:root,lambda:None,root/'index.json');sid=repository.register(path)['session_id']
            personal=PersonalTerms(lambda:root/'profile'/'terms.tsv')
            mapping=MappingStore(lambda:[])
            service=ReviewService(lambda:repository,LocalSpelling(app.BASE/'dictionaries'),personal,mapping,lambda _:None)
            with patch.object(app,'repository',repository),patch.object(app,'personal_terms',personal),patch.object(app,'reviews',service):
                client=TestClient(app.app)
                self.assertEqual(client.get('/sessions/'+sid+'/suspects').json()['state'],'idle')
                self.assertFalse(service.path(sid).exists())
                self.assertEqual(client.post('/sessions/'+sid+'/suspects/search').status_code,200)
                service.threads[sid].join(10)
                data=client.get('/sessions/'+sid+'/suspects').json();self.assertEqual(data['pending'],1)
                response=client.post(f"/sessions/{sid}/suspects/{data['rows'][0]['id']}/decision",json={'revision':data['revision'],'action':'accept'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(client.get('/dictionary/personal-terms').json()['entries'][0]['term'],'xqzblorf')


if __name__=='__main__':unittest.main()
