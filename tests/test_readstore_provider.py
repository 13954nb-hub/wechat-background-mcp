from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
import sys
from unittest.mock import patch

from wxbg import readstore_provider as provider


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        for name in ('session/session.db', 'contact/contact.db'):
            path = self.path/name
            path.parent.mkdir()
            path.write_bytes(b'S'*4096)
        self.account = SimpleNamespace(username='owned',cfg_pointer=10)
        self.closed = []
        owner = self
        class Reader:
            module_base=0x10000
            module_size=4096
            read = lambda *args: None
            regions = lambda *args: []
            def __enter__(self): return self
            def __exit__(self,*args): owner.closed.append('reader')
            def revalidate(self): return {'stable':True}
        location=SimpleNamespace(path=self.path,account_path=self.path,
            storage_id=(1,2),account_id=(1,3),revalidate=lambda:None)
        capture=SimpleNamespace(database=b'S'*4096,wal=b'',wal_index=b'I'*136,evidence={'atomic_snapshot':False})
        @contextmanager
        def opening(*args,**kwargs):
            try: yield object(), {'integrity_verified':True}
            finally: self.closed.append('database')
        self.mocks = {}
        for name, kwargs in (
            ('ProcessReader', {'return_value':Reader()}),
            ('locate_account', {'return_value':self.account}),
            ('discover_roots', {'return_value':[self.path]}),
            ('select_storage', {'return_value':location}),
            ('capture_quiet', {'return_value':capture}),
            ('find_database_keys', {'return_value':({'session':b'K'*32,'contact':b'K'*32},{'verified_databases':2})}),
            ('open_snapshot', {'side_effect':opening}),
        ):
            patcher=patch.object(provider,name,**kwargs)
            self.mocks[name]=patcher.start()
            self.addCleanup(patcher.stop)

    def load(self):
        return provider.open_stores({'pid':1,'created':2,'hwnd':3},deadline=time.monotonic()+2)

    def test_complete_pipeline_and_context_cleanup(self):
        with self.load() as value:
            self.assertEqual(set(value['connections']), {'session','contact'})
            self.assertEqual(len(value['account_epoch']),64)
            self.assertFalse(value['evidence']['multi_database_atomic'])
        self.assertEqual(self.closed, ['database','database','reader'])
        self.assertEqual(self.mocks['capture_quiet'].call_count,4)

    def test_account_switch_after_query_invalidates_result(self):
        self.mocks['locate_account'].side_effect=[self.account,SimpleNamespace(username='other',cfg_pointer=11)]
        with self.assertRaisesRegex(provider.ProviderError,'^readstore_account_changed$'):
            with self.load(): pass
        self.assertEqual(self.closed,['database','database','reader'])

    def test_file_replacement_after_query_rejected(self):
        with self.assertRaisesRegex(provider.ProviderError,'^readstore_source_changed$'):
            with self.load():
                db=self.path/'session/session.db'
                db.rename(db.with_suffix('.old'))
                db.write_bytes(b'S'*4096)

    def test_no_partial_yield_when_key_lookup_fails(self):
        self.mocks['find_database_keys'].side_effect=RuntimeError('secret sentinel')
        with self.assertRaisesRegex(provider.ProviderError,'^readstore_unavailable$'):
            with self.load(): self.fail('must not yield')
        self.assertEqual(self.closed,['reader'])

    def test_run_inbox_waits_for_account_revalidation(self):
        query=SimpleNamespace(read_inbox=lambda *a,**k: {'conversations':[], 'next_cursor':None})
        with patch.dict(sys.modules, {'wxbg.readstore_inbox':query}):
            result=provider.run('readstore_read_inbox',{},target={'pid':1,'created':2,'hwnd':3},deadline=time.monotonic()+2)
        self.assertEqual(result['conversations'],[])
        self.assertEqual(self.closed,['database','database','reader'])

    def test_run_inbox_forwards_include_hidden_after_validation(self):
        calls=[]
        def read_inbox(*args, **kwargs):
            calls.append(kwargs)
            return {'conversations':[], 'next_cursor':None}
        query=SimpleNamespace(read_inbox=read_inbox)
        with patch.dict(sys.modules, {'wxbg.readstore_inbox':query}):
            provider.run('readstore_read_inbox',{'include_hidden':True},
                         target={'pid':1,'created':2,'hwnd':3},
                         deadline=time.monotonic()+2)
        self.assertEqual(len(calls),1)
        self.assertIs(calls[0]['include_hidden'],True)

    def test_invalid_args_fail_before_process_open(self):
        for args in ({'limit':True},{'unread_only':1},
                     {'include_hidden':1},{'path':'anything'}):
            with self.subTest(args=args),self.assertRaisesRegex(provider.ProviderError,'^readstore_input_invalid$'):
                provider.run('readstore_read_inbox',args,target={},deadline=time.monotonic()+2)
        self.mocks['ProcessReader'].assert_not_called()

    def test_run_does_not_return_query_result_after_account_switch(self):
        self.mocks['locate_account'].side_effect=[self.account,SimpleNamespace(username='other',cfg_pointer=11)]
        query=SimpleNamespace(read_inbox=lambda *a,**k: {'conversations':[{'summary':'owned sentinel'}]})
        with patch.dict(sys.modules, {'wxbg.readstore_inbox':query}):
            with self.assertRaisesRegex(provider.ProviderError,'^readstore_account_changed$'):
                provider.run('readstore_read_inbox',{},target={},deadline=time.monotonic()+2)
