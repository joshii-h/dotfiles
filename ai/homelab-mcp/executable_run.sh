#!/usr/bin/env bash
# Startet den Homelab-MCP-Server (SSE :8766). Nutzt das media-venv (hat FastMCP).
set -euo pipefail
exec /home/joshii/ai/venv/bin/python /home/joshii/ai/homelab-mcp/server.py
