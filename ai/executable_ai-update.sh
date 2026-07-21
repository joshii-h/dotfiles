#!/usr/bin/env bash
# AI-Stack-Update — wird vom parallelen "ai"-Job in sysup() aufgerufen.
# Aktualisiert NUR den App-Layer (diffusers/transformers/ComfyUI/custom_nodes).
# torch/vision/audio/triton bleiben ROCm-gepinnt (torch-constraints.txt) — nie CUDA.
# System-ROCm-Libs (rocBLAS, MIOpen, …) kommen aus Portage und laufen über @world.
# chezmoi-managed: ~/.local/share/chezmoi/ai/executable_ai-update.sh
set -uo pipefail
AI="${HOME}/ai"
VENV="${AI}/venv"
PIP="${VENV}/bin/pip"
CONSTR="${AI}/torch-constraints.txt"

[[ -x "$PIP" ]] || { echo "kein venv (${VENV}) — zuerst 'chezmoi apply' (setup-ai.sh baut ihn)"; exit 0; }

pipu() { "$PIP" install --upgrade --disable-pip-version-check -c "$CONSTR" "$@"; }

echo ">> App-Layer aktualisieren (torch bleibt gepinnt)"
pipu diffusers transformers accelerate safetensors huggingface_hub \
     librosa soundfile 'mcp[cli]' uvicorn 2>&1 | tail -4

if [[ -d "${AI}/ComfyUI/.git" ]]; then
  echo ">> ComfyUI aktualisieren"
  git -C "${AI}/ComfyUI" pull --ff-only 2>&1 | tail -2
  [[ -f "${AI}/ComfyUI/requirements.txt" ]] && \
    pipu -r "${AI}/ComfyUI/requirements.txt" 2>&1 | tail -2

  echo ">> ComfyUI custom_nodes aktualisieren"
  for d in "${AI}/ComfyUI/custom_nodes"/*/; do
    [[ -d "${d}.git" ]] || continue
    echo "   - $(basename "$d")"
    git -C "$d" pull --ff-only 2>&1 | tail -1
    [[ -f "${d}requirements.txt" ]] && pipu -r "${d}requirements.txt" 2>&1 | tail -1
  done
fi

# --- Odysseus (Docker-Frontend gegen lokales Ollama) ---
if [[ -d "${AI}/odysseus/.git" ]] && command -v docker >/dev/null; then
  echo ">> Odysseus aktualisieren"
  # tool_parsing.py traegt unseren qwen3-function-tag-Patch (qwen3-coder
  # Text-Tool-Calls). Vor dem Pull auf HEAD zuruecksetzen, damit --ff-only
  # nicht an lokalen Aenderungen scheitert, dann nach dem Pull neu anwenden.
  git -C "${AI}/odysseus" checkout -- src/tool_parsing.py 2>/dev/null
  git -C "${AI}/odysseus" pull --ff-only 2>&1 | tail -1
  # Rebuild NUR wenn der Patch sauber sitzt (set -o pipefail -> if sieht den
  # Patcher-Exitcode, nicht tail). Scheitert der Patch (Anchor weg nach upstream-
  # Refactor), NICHT bauen -> altes, GEPATCHTES Image laeuft weiter, statt ein
  # ungepatchtes zu backen (das qwen3-Tool-Calls still wieder kaputtmachen wuerde).
  if python3 "${AI}/odysseus-patches/qwen3_function_tag_patch.py" \
       "${AI}/odysseus/src/tool_parsing.py" 2>&1 | tail -2; then
    ( cd "${AI}/odysseus" && docker compose up -d --build ) 2>&1 | tail -3
  else
    echo "!! WARNUNG: qwen3-function-tag-Patch NICHT angewendet (Anchor fehlt?) -> Rebuild uebersprungen, altes gepatchtes Image bleibt aktiv"
  fi
fi

# --- Hermes Agent (nativ, uv-venv gegen lokales Ollama) ---
if [[ -d "${AI}/hermes-agent/.git" ]] && command -v uv >/dev/null; then
  echo ">> Hermes aktualisieren"
  git -C "${AI}/hermes-agent" pull --ff-only 2>&1 | tail -1
  ( cd "${AI}/hermes-agent" && uv pip install --python .venv/bin/python -e ".[cli,mcp,cron]" ) 2>&1 | tail -2
fi

# --- Media-MCP-Server (Whisper + SDXL) — chezmoi hat server.py evtl. aktualisiert ---
if [[ -f /etc/init.d/media-mcp ]] && command -v sudo >/dev/null; then
  echo ">> media-mcp neustarten (aktualisierten Code laden)"
  sudo rc-service media-mcp restart 2>&1 | tail -1
  # Odysseus' SSE-Verbindung wird durch den Restart abgestanden -> neu verbinden,
  # damit transcribe/generate_image im Chat weiter funktionieren.
  if [[ -d "${AI}/odysseus/.git" ]] && command -v docker >/dev/null; then
    echo ">> Odysseus neu verbinden (frische Media-MCP-SSE-Verbindung)"
    sleep 3
    ( cd "${AI}/odysseus" && docker compose restart odysseus ) 2>&1 | tail -1
  fi
fi

# Sanity: torch muss ROCm-Build bleiben (kein GPU-Init, damit ohne render-Gruppe ok)
v="$("${VENV}/bin/python" -c 'import torch;print(torch.__version__)' 2>/dev/null)"
if [[ "$v" == *rocm* ]]; then
  echo ">> torch OK: $v"
else
  echo "!! WARNUNG: torch ist '$v' — KEIN ROCm-Build! setup-ai.sh erneut ausführen."
fi
echo "AI-Update fertig."
