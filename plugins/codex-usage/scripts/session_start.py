"""Quick trusted hook: attach/start a collector, then return to Codex."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'runtime'))
os.environ['CODEX_USAGE_CONFIG'] = str(Path(__file__).resolve().parents[1]/'runtime.json')
from codex_usage.plugin_mcp import configured_client
from codex_usage.collector import log_event

client = configured_client()
request = json.loads(sys.stdin.read(65536) or '{}')
source = request.get('source')
source = source if source in ('startup', 'resume', 'clear', 'compact') else 'unknown'
log_event(client.config, 'hook_invocation', source=source)
try:
    result = client.start(cause='SessionStart:'+source)
    message = 'Codex Usage collector attached; statistics use account telemetry and per-thread calculation baselines.'
except (OSError, ValueError):
    message = 'Codex Usage collector could not start. Mention Codex Usage and request start for a diagnostic.'
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': message}}))
