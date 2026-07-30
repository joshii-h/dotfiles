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
     librosa soundfile 'mcp[cli]' uvicorn pyannote.audio 2>&1 | tail -4

# torchcodec IMMER entfernen: pyannote.audio zieht es als Dep rein, aber der
# PyPI-Build ist CUDA (libnvrtc) -> sein fehlschlagendes torch.ops.load_library
# beschaedigt die GPU-Op-Registry -> Whisper UND pyannote brechen mit roctracer-
# SIGABRT ab. Wir laden Audio selbst via soundfile -> torchcodec unnoetig.
"$VENV/bin/pip" uninstall -y torchcodec >/dev/null 2>&1 && echo ">> torchcodec entfernt (GPU-Op-Registry-Schutz)" || true

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

# --- Standalone-SearXNG (entkoppelt von Odysseus, 2026-07-25) ---
# Odysseus wurde entfernt -> alles laeuft ueber Hermes. SearXNG ist jetzt ein
# eigenstaendiger Ein-Container-Stack (~/ai/searxng) und dient als Such-Backend
# fuer media-mcp `deep_research` + Hermes `web_search`. Kein Patchen mehr noetig
# (eigene settings.yml). restart:unless-stopped -> startet mit dem Docker-Daemon;
# hier nur sicherstellen, dass es laeuft (Image ist gepinnt, kein --build/pull).
if [[ -f "${AI}/searxng/docker-compose.yml" ]] && command -v docker >/dev/null; then
  echo ">> SearXNG (standalone) sicherstellen"
  ( cd "${AI}/searxng" && docker compose up -d ) 2>&1 | tail -2
fi

# --- Hermes Agent (nativ, uv-venv gegen lokales Ollama) ---
if [[ -d "${AI}/hermes-agent/.git" ]] && command -v uv >/dev/null; then
  echo ">> Hermes aktualisieren"
  # agent/transports/chat_completions.py traegt unseren qwen-function-eq-Patch:
  # Fallback-Parser fuer text-format <function=NAME><parameter=K>V</parameter>
  # </function>-Tool-Calls, die qwen3-coder/Ollama gelegentlich statt nativer
  # tool_calls in den content leakt (sonst landet der rohe Block als "Antwort").
  # Vor dem Pull auf HEAD zuruecksetzen, damit --ff-only nicht an der lokalen
  # Aenderung scheitert, dann nach dem Pull neu anwenden. Hermes ist ein
  # editable-install -> die gepatchte Datei ist sofort live (kein Rebuild).
  # Anders als Odysseus gibt es keine Build-Isolation: schlaegt der Patch fehl
  # (Anchor weg nach upstream-Refactor), laeuft Hermes ungepatcht weiter -> nur
  # WARNEN (nicht abbrechen), Update trotzdem durchziehen.
  CC="${AI}/hermes-agent/agent/transports/chat_completions.py"
  _ccbak="$(mktemp)"; cp "$CC" "$_ccbak" 2>/dev/null   # zuletzt gepatchte Datei sichern
  git -C "${AI}/hermes-agent" checkout -- agent/transports/chat_completions.py 2>/dev/null
  git -C "${AI}/hermes-agent" pull --ff-only 2>&1 | tail -1
  if python3 "${AI}/odysseus-patches/hermes_function_eq_patch.py" "$CC" 2>&1 | tail -2; then
    rm -f ~/.hermes-patch-missing 2>/dev/null
  else
    # Patch fehlgeschlagen (Anchor weg nach upstream-Refactor ODER Skript fehlt
    # durch chezmoi-Drift): NICHT ungepatcht laufen lassen -> die zuletzt
    # gepatchte Datei wiederherstellen (besser altes gepatchtes Transport-Modul
    # als der wieder offene <function=>-Leak). Bleibt sie trotzdem ungepatcht,
    # ein dauerhaftes Signal setzen (ueberlebt das Scrollen der sysup-Ausgabe).
    echo "!! WARNUNG: qwen-function-eq-Patch NICHT angewendet -> stelle zuletzt gepatchte Datei wieder her"
    cp "$_ccbak" "$CC" 2>/dev/null
    grep -q 'HERMES-PATCH:qwen-function-eq' "$CC" 2>/dev/null \
      || { echo "!! Hermes laeuft UNGEPATCHT (<function=>-Leak aktiv) — siehe ~/.hermes-patch-missing"; touch ~/.hermes-patch-missing; }
  fi
  rm -f "$_ccbak"
  ( cd "${AI}/hermes-agent" && uv pip install --python .venv/bin/python -e ".[cli,mcp,cron]" ) 2>&1 | tail -2
fi

# --- Media-MCP-Server (Whisper + SDXL) — chezmoi hat server.py evtl. aktualisiert ---
if [[ -f /etc/init.d/media-mcp ]] && command -v sudo >/dev/null; then
  echo ">> media-mcp neustarten (aktualisierten Code laden)"
  sudo rc-service media-mcp restart 2>&1 | tail -1
  # Hermes verbindet die media-MCP-SSE beim naechsten Chat automatisch neu
  # (kein separater Reconnect noetig -- Odysseus, das das brauchte, ist weg).
fi

# Sanity: torch muss ROCm-Build bleiben (kein GPU-Init, damit ohne render-Gruppe ok)
v="$("${VENV}/bin/python" -c 'import torch;print(torch.__version__)' 2>/dev/null)"
if [[ "$v" == *rocm* ]]; then
  echo ">> torch OK: $v"
else
  echo "!! WARNUNG: torch ist '$v' — KEIN ROCm-Build! setup-ai.sh erneut ausführen."
fi
echo "AI-Update fertig."
