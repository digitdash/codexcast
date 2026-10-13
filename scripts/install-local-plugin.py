#!/usr/bin/env python3
"""Build/install a local proof with bundled source and no credential access."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def install(args):
    repo = Path(__file__).resolve().parents[1]
    destination = Path(args.destination).expanduser().resolve()
    runtime = Path(args.runtime).expanduser().resolve()
    marketplace = Path(args.marketplace).expanduser().resolve()
    root = marketplace.parent.parent.parent
    relative = destination.relative_to(root)
    entry = {'name': 'codex-usage', 'source': {'source': 'local', 'path': './'+str(relative)},
             'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
             'category': 'Developer Tools'}
    data = json.loads(marketplace.read_text()) if marketplace.exists() else {
        'name': 'local-codex-usage', 'interface': {'displayName': 'Local Codex Usage'}, 'plugins': []}
    matches = [p for p in data['plugins'] if p['name'] == 'codex-usage']
    if matches and matches[0]['source'] != entry['source']:
        raise ValueError('A different Codex Usage plugin exists in this marketplace')
    if destination.exists():
        manifest = destination/'.codex-plugin/plugin.json'
        if not manifest.exists() or json.loads(manifest.read_text()).get('name') != 'codex-usage':
            raise ValueError('Refusing to overwrite a directory that is not this plugin')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (runtime/'bin/python').exists():
        subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True)
    python = runtime/'bin/python'
    subprocess.run([str(python), '-m', 'pip', 'install', 'mcp>=1.12.4,<2'], check=True)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Remove launchers from older proof builds so they cannot shadow SDK modules.
    if (destination/'scripts').exists(): shutil.rmtree(destination/'scripts')
    shutil.copytree(repo/'plugins/codex-usage', destination, dirs_exist_ok=True)
    bundled = destination/'runtime/codex_usage'
    if bundled.exists(): shutil.rmtree(bundled)
    shutil.copytree(repo/'src/codex_usage', bundled, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    configuration = {'db': str(Path(args.db).expanduser().resolve()),
                     'codex_bin': str(Path(args.codex_bin).expanduser().resolve()),
                     'codex_home': str(Path(args.codex_home).expanduser().resolve())}
    (destination/'runtime.json').write_text(json.dumps(configuration, indent=2)+'\n')
    (destination/'.mcp.json').write_text(json.dumps({'mcpServers': {'codex_usage': {
        'command': str(python), 'args': ['./scripts/serve.py'], 'cwd': '.'}}}, indent=2)+'\n')
    hooks = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command',
             'command': shlex.quote(str(python))+' "${PLUGIN_ROOT}/scripts/session_start.py"',
             'statusMessage': 'Starting local usage collector'}]}]}}
    (destination/'hooks').mkdir(exist_ok=True)
    (destination/'hooks/hooks.json').write_text(json.dumps(hooks, indent=2)+'\n')
    data['plugins'] = [p for p in data['plugins'] if p['name'] != 'codex-usage']+[entry]
    marketplace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if marketplace.exists():
        backup = marketplace.with_name('marketplace.before-codex-usage.json')
        if not backup.exists(): shutil.copy2(marketplace, backup)
    marketplace.write_text(json.dumps(data, indent=2)+'\n')
    print(json.dumps({'plugin': str(destination), 'marketplace': str(marketplace),
                      'marketplace_name': data['name'], 'python': str(python)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    support = Path.home()/'Library/Application Support/codex-usage-estimator'
    parser.add_argument('--destination', default=str(Path.home()/'.codex/local-plugins/codex-usage'))
    parser.add_argument('--runtime', default=str(support/'plugin-runtime'))
    parser.add_argument('--marketplace', default=str(Path.home()/'.agents/plugins/marketplace.json'))
    parser.add_argument('--db', default=str(support/'usage.sqlite3'))
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
    parser.add_argument('--codex-bin', required=True)
    install(parser.parse_args())
