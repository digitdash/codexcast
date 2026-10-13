#!/usr/bin/env python3
"""Exercise the installed plugin through two real bundled app-server sessions.

No turns or model requests are submitted. Output is allowlisted local proof data;
keep account measurements in a private file rather than committing them.
"""
import argparse
import json
from pathlib import Path
import time
from codex_usage.provider import CodexProvider


class Session:
    def __init__(self, binary, cwd):
        self.rpc = CodexProvider(binary, timeout=60)
        self.rpc._start()
        self.thread = self.rpc._request('thread/start', {'cwd': cwd, 'ephemeral': True})['thread']['id']
        self.inventory = self.rpc._request('mcpServerStatus/list', {'threadId': self.thread, 'serverName': 'codex_usage'})

    def tool(self, name, arguments=None):
        result = self.rpc._request('mcpServer/tool/call', {'threadId': self.thread, 'server': 'codex_usage',
                                   'tool': name, 'arguments': arguments or {}})
        if result.get('isError'):
            raise RuntimeError('Installed plugin tool failed: '+str(result.get('content'))[:500])
        return result['structuredContent']

    def close(self): self.rpc.close()


def main(args):
    first = Session(args.codex_bin, args.cwd)
    second = None
    try:
        plugin = first.rpc._request('plugin/read', {'pluginName': 'codex-usage',
                     'marketplacePath': str(Path.home()/'.agents/plugins/marketplace.json')})['plugin']
        root = Path(plugin['summary']['source']['path'])
        runtime = json.loads((root/'runtime.json').read_text())
        if Path(runtime['db']).resolve() != Path(args.expected_db).expanduser().resolve():
            raise ValueError('Installed plugin is configured for a different database')
        report = {'client': args.codex_bin, 'model_requests': 0, 'ephemeral_sessions': 2,
                  'plugin_installed': plugin['summary']['installed'], 'plugin_enabled': plugin['summary']['enabled'],
                  'interface_display_name': plugin['summary']['interface']['displayName'],
                  'skills': [s['name'] for s in plugin['skills']], 'mcp_inventory': first.inventory}
        hooks = first.rpc._request('hooks/list', {'cwds': [args.cwd]})
        report['hooks'] = [h for d in hooks['data'] for h in d['hooks'] if h.get('pluginId') == 'codex-usage@local-codex-usage']
        report['before_start'] = first.tool('usage_stats', {'session_id': first.thread})
        report['start'] = first.tool('usage_start')
        before = first.tool('usage_stats', {'session_id': first.thread})
        print(json.dumps({'phase': 'started', 'collector_id': before['collector_id'],
                          'successful_polls': before['successful_polls']}), flush=True)
        second = Session(args.codex_bin, args.cwd)
        attached = second.tool('usage_start')
        assert attached['collector_id'] == before['collector_id']
        # One sleep and no stats/model calls during the entire observation gap.
        started = time.monotonic();time.sleep(args.wait_seconds)
        after = second.tool('usage_stats', {'session_id': second.thread})
        report.update(before=before, after=after, wait_seconds=time.monotonic()-started)
        assert after['collector_id'] == before['collector_id']
        assert after['provider_pid'] == before['provider_pid']
        assert after['successful_polls']-before['successful_polls'] >= int(args.wait_seconds/30)
        assert after['latest_sample_timestamp'] > before['latest_sample_timestamp']
        assert after['sample_count'] > before['sample_count']
        if args.exercise_resets:
            first.tool('usage_reset_current', {'session_id': first.thread})
            a = first.tool('usage_stats', {'session_id': first.thread})
            b = second.tool('usage_stats', {'session_id': second.thread})
            assert a['calculation_since'] is not None and b['calculation_since'] is None
            first.tool('usage_reset_all')
            a = first.tool('usage_stats', {'session_id': first.thread})
            b = second.tool('usage_stats', {'session_id': second.thread})
            assert a['calculation_since'] == b['calculation_since']
            assert a['sample_count'] >= after['sample_count']
            report['reset_current_isolated'] = report['reset_all_shared'] = report['history_preserved'] = True
        first.close()
        report['after_session_close'] = second.tool('usage_stats', {'session_id': second.thread})
        assert report['after_session_close']['collector_id'] == before['collector_id']
        second.tool('usage_stop')
        print(json.dumps(report, indent=2, allow_nan=False), flush=True)
    finally:
        if second is not None:second.close()
        first.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-bin', required=True)
    parser.add_argument('--expected-db', required=True)
    parser.add_argument('--cwd', default=str(Path.cwd()))
    parser.add_argument('--wait-seconds', type=float, default=96)
    parser.add_argument('--exercise-resets', action='store_true')
    main(parser.parse_args())
