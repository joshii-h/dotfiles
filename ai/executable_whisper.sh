#!/usr/bin/env bash
# Whisper-STT-Wrapper — RX 7900 XTX (gfx1100)
#   whisper.sh datei.wav            → DE/EN auto-detect (large-v3)
#   whisper.sh --de datei.wav       → Hochdeutsch erzwingen
#   whisper.sh --en datei.wav       → Englisch erzwingen
#   whisper.sh --swiss datei.wav    → Schweizerdeutsch-Finetune (Output = Hochdeutsch)
# (wird später chezmoi-managed als dot_ai/executable_whisper.sh)
set -euo pipefail
export HSA_TOOLS_LIB=""
export MIOPEN_FIND_MODE=2
export MIOPEN_USER_DB_PATH="${HOME}/ai/.miopen"

MODEL="openai/whisper-large-v3"
LANG=""
args=()
for a in "$@"; do
  case "$a" in
    --swiss) MODEL="Flurin17/whisper-large-v3-turbo-swiss-german"; LANG="de" ;;
    --de) LANG="de" ;;
    --en) LANG="en" ;;
    *) args+=("$a") ;;
  esac
done

exec "${HOME}/ai/venv/bin/python" "${HOME}/ai/transcribe.py" \
  "${args[@]}" --model "$MODEL" ${LANG:+--lang "$LANG"}
