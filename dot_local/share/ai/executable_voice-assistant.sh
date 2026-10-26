#!/usr/bin/env bash
# Launcher für den lokalen deutschen Sprachassistenten.
# HSA_ENABLE_SDMA=0 umgeht den roctracer-Shutdown-Abort (wie media-mcp/run.sh).
export HSA_ENABLE_SDMA=0
exec /home/joshii/.local/share/ai/venv/bin/python /home/joshii/.local/share/ai/voice-assistant.py "$@"
