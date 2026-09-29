import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from test_gateway import load_isolated_gateway


class InboxGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway=load_isolated_gateway(Path(self.tmp.name))

    def test_readonly_schema_and_strict_extra_arguments(self):
        tools=asyncio.run(self.gateway.SERVER.list_tools())
        tool=next(t for t in tools if t.name=='wechat_read_inbox')
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.openWorldHint)
        self.assertFalse(tool.inputSchema.get('additionalProperties',True))
        self.assertEqual(tool.inputSchema['properties']['limit']['maximum'],100)

    def test_dispatch_uses_public_boundary_and_no_journal(self):
        boundary=Mock(return_value={'ok':True,'result':{'conversations':[]}})
        module=types.SimpleNamespace(inbox_result_or_error=boundary)
        response={'ok':True,'result':{}}
        with patch.dict(sys.modules,{'wxbg.readstore_public':module}),patch.object(self.gateway,'execute',return_value=response) as execute:
            result=self.gateway.wechat_read_inbox(limit=10,unread_only=True)
        execute.assert_called_once_with('readstore_read_inbox',{'limit':10,'unread_only':True,'cursor':None})
        boundary.assert_called_once_with(response,limit=10,unread_only=True)
        self.assertTrue(result['ok'])
        self.assertFalse((Path(self.tmp.name)/'operations.sqlite3').exists())

    def test_invalid_direct_inputs_do_not_start_worker(self):
        with patch.object(self.gateway,'execute') as execute:
            for args in ({'limit':True},{'limit':101},{'unread_only':1},{'cursor':[]},{'cursor':{'x':'x'*9000}}):
                with self.subTest(args=list(args)),self.assertRaises(ToolError):
                    self.gateway.wechat_read_inbox(**args)
        execute.assert_not_called()

    def test_actual_mcp_validation_rejects_extra_and_coercion(self):
        with patch.object(self.gateway,'execute') as execute:
            for args in ({'limit':True},{'unread_only':'yes'},{'path':'anything'}):
                with self.subTest(args=args),self.assertRaises(Exception):
                    asyncio.run(self.gateway.SERVER.call_tool('wechat_read_inbox',args))
        execute.assert_not_called()

    def test_list_conversations_uses_all_sessiontable_rows_without_ui_refs(self):
        capabilities=self.gateway.wechat_capabilities()
        self.assertIn('all_sessiontable_conversations',capabilities['implemented'])
        self.assertEqual(capabilities['list_conversations']['coverage'],'all_sessiontable_rows')
        tools=asyncio.run(self.gateway.SERVER.list_tools())
        tool=next(t for t in tools if t.name=='wechat_list_conversations')
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.inputSchema.get('additionalProperties',True))
        boundary=Mock(return_value={'ok':True,'result':{'conversations':[],
                            'coverage':'sessiontable_contact_bounded_all'}})
        module=types.SimpleNamespace(inbox_result_or_error=boundary)
        response={'ok':True,'result':{}}
        cursor={'version':2,'include_hidden':True}
        with patch.dict(sys.modules,{'wxbg.readstore_public':module}),patch.object(
                self.gateway,'execute',return_value=response) as execute:
            result=self.gateway.wechat_list_conversations(limit=10,cursor=cursor)
        execute.assert_called_once_with('readstore_read_inbox',{
            'limit':10,'unread_only':False,'include_hidden':True,'cursor':cursor})
        boundary.assert_called_once_with(response,limit=10,unread_only=False,
                                         include_hidden=True)
        self.assertTrue(result['ok'])
        self.assertFalse((Path(self.tmp.name)/'operations.sqlite3').exists())

    def test_list_conversations_rejects_invalid_inputs_before_worker(self):
        with patch.object(self.gateway,'execute') as execute:
            for args in ({'limit':True},{'limit':101},{'cursor':[]},
                         {'cursor':{'x':'x'*9000}}):
                with self.subTest(args=list(args)),self.assertRaises(ToolError):
                    self.gateway.wechat_list_conversations(**args)
        execute.assert_not_called()
