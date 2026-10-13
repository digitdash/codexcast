#!/usr/bin/env python3
"""Install Codex Usage from this checkout without copying credentials or history."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def find_codex(override=None):
    """Prefer the desktop's bundled client over an unrelated CLI on PATH."""
    if override:
        supplied = Path(override).expanduser()
        candidate = supplied if supplied.is_file() else Path(shutil.which(override) or supplied)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise ValueError('The --codex-bin path must point to an executable Codex binary')
    for directory in (Path.home() / 'Applications', Path('/Applications')):
        for app in ('ChatGPT.app', 'Codex.app'):
            for executable in ('codex-cli/CodexCLI.app/Contents/MacOS/codex', 'codex'):
                candidate = directory / app / 'Contents/Resources' / executable
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate.resolve())
    candidate = shutil.which('codex')
    if candidate:
        return str(Path(candidate).resolve())
    raise ValueError('Codex was not found. Install the desktop app or pass --codex-bin /path/to/codex')


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
    return {'plugin': str(destination), 'marketplace': str(marketplace),
            'marketplace_name': data['name'], 'python': str(python)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    support = Path.home()/'Library/Application Support/codex-usage-estimator'
    parser.add_argument('--destination', default=str(Path.home()/'.codex/local-plugins/codex-usage'),
                        help='Stable plugin source folder inside the marketplace root')
    parser.add_argument('--runtime', default=str(support/'plugin-runtime'), help='Separate Python dependency runtime')
    parser.add_argument('--marketplace', default=str(Path.home()/'.agents/plugins/marketplace.json'),
                        help='Marketplace catalog; unrelated entries are preserved')
    parser.add_argument('--db', default=str(support/'usage.sqlite3'), help='Usage history; use a separate file per account')
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')),
                        help='Codex home used for plugin installation and telemetry authentication')
    parser.add_argument('--codex-bin', help='Override automatic desktop executable discovery')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise ValueError('Python 3.11 or newer is required; run this installer with a newer Python')
        if sys.platform != 'darwin':
            raise ValueError('Codex Usage currently supports macOS desktop clients only')
        args.codex_bin = find_codex(args.codex_bin)
        env = dict(os.environ, CODEX_HOME=str(Path(args.codex_home).expanduser().resolve()))
        supported = subprocess.run([args.codex_bin, 'plugin', 'add', '--help'], env=env,
                                   capture_output=True, text=True, timeout=15)
        if supported.returncode:
            raise ValueError('This Codex binary does not support local plugins. Update the desktop app '
                             'or use --codex-bin to select its bundled executable')
        result = install(args)
        root = str(Path(result['marketplace']).parent.parent.parent)
        subprocess.run([args.codex_bin, 'plugin', 'marketplace', 'add', root], env=env,
                       capture_output=True, text=True, check=True, timeout=60)
        subprocess.run([args.codex_bin, 'plugin', 'add', 'codex-usage@'+result['marketplace_name'], '--json'],
                       env=env, capture_output=True, text=True, check=True, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Setup failed: '+str(error), file=sys.stderr)
        return 1
    print('\nInstalled Codex Usage.\n'
          '1. Restart the desktop app and open a new local Codex chat.\n'
          '2. Select Codex Usage from the @ picker, then request start and stats.\n'
          '3. Review the SessionStart hook in Codex to enable automatic startup.\n'
          '   Explicit start also works when the hook is not trusted.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
