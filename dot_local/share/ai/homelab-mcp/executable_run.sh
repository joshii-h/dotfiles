#!/usr/bin/env bash
# Startet den Homelab-MCP-Server (SSE :8766). Nutzt das media-venv (hat FastMCP).
set -euo pipefail
exec /home/joshii/.local/share/ai/venv/bin/python /home/joshii/.local/share/ai/homelab-mcp/server.py
