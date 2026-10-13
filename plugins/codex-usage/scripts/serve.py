"""Installed server launcher; avoid shadowing the MCP SDK module name."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'runtime'))
os.environ['CODEX_USAGE_CONFIG'] = str(Path(__file__).resolve().parents[1]/'runtime.json')
from codex_usage.plugin_mcp import main
main()
