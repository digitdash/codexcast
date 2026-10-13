import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
try:
    from codex_usage import plugin_mcp
except ImportError:
    plugin_mcp=None


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional plugin extra for MCP tests')
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_launcher_initializes_without_module_shadowing(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import json,shutil,sys
        with tempfile.TemporaryDirectory(prefix='mcp-') as temp:
            root=Path(temp)
            repo=Path(__file__).resolve().parents[1]
            plugin=root/'plugin'
            shutil.copytree(repo/'plugins/codex-usage',plugin)
            shutil.copytree(repo/'src/codex_usage',plugin/'runtime/codex_usage')
            (plugin/'runtime.json').write_text(json.dumps({'db':str(root/'db'),
                'codex_home':str(root/'home'),'runtime_root':str(root/'runtime')}))
            script=next(p for p in (plugin/'scripts').glob('*.py') if p.name!='session_start.py')
            params=StdioServerParameters(command=sys.executable,args=[str(script)],cwd=temp)
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool('usage_stats',{'session_id':'a'})
                    self.assertEqual(result.structuredContent['state'],'stopped')

    async def test_initialization_tools_and_invalid_input_without_collection(self):
        self.assertIsNotNone(plugin_mcp,'MCP server is missing')
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory(prefix='mcp-') as temp:
            env=dict(os.environ,CODEX_USAGE_CONFIG=str(Path(temp)/'config.json'))
            import json,sys
            path=Path(temp)/'config.json'
            path.write_text(json.dumps({'db':str(Path(temp)/'db'),'codex_home':str(Path(temp)/'home'),
                                         'runtime_root':str(Path(temp)/'runtime')}))
            params=StdioServerParameters(command=sys.executable,args=['-m','codex_usage.plugin_mcp'],env=env)
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    names={t.name for t in (await session.list_tools()).tools}
                    self.assertEqual(names,{'usage_start','usage_stats','usage_reset_current','usage_reset_all','usage_stop'})
                    stats=await session.call_tool('usage_stats',{'session_id':'a'})
                    self.assertEqual(stats.structuredContent['state'],'stopped')
                    invalid=await session.call_tool('usage_reset_current',{'session_id':''})
                    self.assertTrue(invalid.isError)
                    self.assertFalse((Path(temp)/'db').exists())

    async def test_tools_share_collector_and_resets_across_mcp_reconnect(self):
        self.assertIsNotNone(plugin_mcp,'MCP server is missing')
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from codex_usage.collector import CollectorClient,CollectorConfig
        from codex_usage.lifecycle import process_identity
        from test_plugin_collector import FAKE
        import json,sys
        with tempfile.TemporaryDirectory(prefix='mcp-') as temp:
            root=Path(temp);fake=root/'codex';fake.write_text(FAKE);fake.chmod(0o700)
            config=CollectorConfig(root/'db',str(fake),root/'home',runtime_root=root/'runtime')
            owner=subprocess.Popen(['sleep','60'])
            client=CollectorClient(config,owner=process_identity(owner.pid))
            path=root/'config.json';path.write_text(json.dumps(config.wire()))
            params=StdioServerParameters(command=sys.executable,args=['-m','codex_usage.plugin_mcp'],
                                         env=dict(os.environ,CODEX_USAGE_CONFIG=str(path)))
            try:
                started=client.start()
                for index in range(2):
                    async with stdio_client(params) as (read,write):
                        async with ClientSession(read,write) as session:
                            await session.initialize()
                            result=await session.call_tool('usage_stats',{'session_id':'a'})
                            self.assertEqual(result.structuredContent['collector_id'],started['collector_id'])
                            if index==0:
                                await session.call_tool('usage_reset_current',{'session_id':'a'})
                            else:
                                self.assertIsNotNone(result.structuredContent['calculation_since'])
                                other=await session.call_tool('usage_stats',{'session_id':'b'})
                                self.assertIsNone(other.structuredContent['calculation_since'])
                                await session.call_tool('usage_reset_all',{})
                                other=await session.call_tool('usage_stats',{'session_id':'b'})
                                self.assertIsNotNone(other.structuredContent['calculation_since'])
                                stopped=await session.call_tool('usage_stop',{})
                                self.assertEqual(stopped.structuredContent['state'],'stopped')
            finally:client.stop();owner.terminate();owner.wait()
