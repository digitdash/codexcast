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
import tempfile


def find_codex(override=None):
    """Prefer a plugin-capable desktop client, then a supported CLI on PATH."""
    candidates = []
    if override:
        supplied = Path(override).expanduser()
        candidate = supplied if supplied.is_file() else Path(shutil.which(override) or supplied)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append(str(candidate.resolve()))
        else:
            raise ValueError('The --codex-bin path must point to an executable Codex binary')
    else:
        for directory in (Path.home() / 'Applications', Path('/Applications')):
            for app in ('ChatGPT.app', 'Codex.app'):
                for executable in ('codex-cli/CodexCLI.app/Contents/MacOS/codex', 'codex'):
                    candidate = directory / app / 'Contents/Resources' / executable
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        candidates.append(str(candidate.resolve()))
        candidate = shutil.which('codex')
        if candidate:
            candidates.append(str(Path(candidate).resolve()))
    if not candidates:
        raise ValueError('Codex was not found. Install the desktop app or pass --codex-bin /path/to/codex')
    # A fresh/custom Codex home need not exist yet. Probe capabilities in an
    # empty temporary profile, without touching the user's configuration.
    with tempfile.TemporaryDirectory(prefix='codex-usage-check-') as probe_home:
        env = dict(os.environ, CODEX_HOME=probe_home)
        for candidate in dict.fromkeys(candidates):
            try:
                supported = subprocess.run([candidate, 'plugin', 'add', '--help'], env=env,
                                           capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                continue
            if supported.returncode == 0:
                return candidate
    raise ValueError('No selected Codex binary supports local plugins. Update the desktop app '
                     'or use --codex-bin to select a supported executable')


def make_configuration(args):
    # Reuse the collector's actual path validation; no SDK is needed for this.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from codex_usage.collector import CollectorConfig
    configuration = {'db': str(Path(args.db).expanduser().resolve()),
                     'codex_bin': str(Path(args.codex_bin).expanduser().resolve()),
                     'codex_home': str(Path(args.codex_home).expanduser().resolve())}
    runtime_root = args.collector_runtime
    previous = Path(args.destination).expanduser() / 'runtime.json'
    if runtime_root is None and previous.is_file():
        runtime_root = json.loads(previous.read_text()).get('runtime_root')
    if runtime_root is not None:
        configuration['runtime_root'] = str(Path(runtime_root).expanduser().resolve())
    try:
        CollectorConfig(**configuration)
    except ValueError:
        if runtime_root is not None:
            raise
        # Keep the existing default for compatible installs. Long home paths
        # need a shorter socket directory; the database and key are unchanged.
        configuration['runtime_root'] = str(Path.home() / '.codex/usage-runtime')
        CollectorConfig(**configuration)
    return configuration


def install(args):
    repo = Path(__file__).resolve().parents[1]
    destination = Path(args.destination).expanduser().resolve()
    runtime = Path(args.runtime).expanduser().resolve()
    marketplace = Path(args.marketplace).expanduser().resolve()
    if marketplace.parts[-3:] != ('.agents', 'plugins', 'marketplace.json'):
        raise ValueError('--marketplace must end with .agents/plugins/marketplace.json')
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
    configuration = make_configuration(args)
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
                        help='Catalog path ending in .agents/plugins/marketplace.json; unrelated entries are preserved')
    parser.add_argument('--db', default=str(support/'usage.sqlite3'), help='Usage history; use a separate file per account')
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')),
                        help='Codex home used for plugin installation and telemetry authentication')
    parser.add_argument('--collector-runtime', help='Override the collector socket/baseline directory; existing locations are retained')
    parser.add_argument('--codex-bin', help='Override automatic desktop executable discovery')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise ValueError('Python 3.11 or newer is required; run this installer with a newer Python')
        if sys.platform != 'darwin':
            raise ValueError('Codex Usage currently supports macOS desktop clients only')
        codex_home = Path(args.codex_home).expanduser().resolve()
        if codex_home.exists() and not codex_home.is_dir():
            raise ValueError('--codex-home must name a directory')
        args.codex_bin = find_codex(args.codex_bin)
        env = dict(os.environ, CODEX_HOME=str(codex_home))
        result = install(args)
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
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
