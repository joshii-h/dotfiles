#!/usr/bin/env python3
"""Media-MCP-Server für Battlestation (RX 7900 XTX / ROCm).

Stellt Tools über MCP (SSE) bereit, damit Odysseus/Hermes sie aus dem Chat
heraus aufrufen können:
  - transcribe(file, language)      -> Whisper-Transkript (ffmpeg-Demux, auch mp4/Video)
  - generate_image(prompt, ...)     -> Bild via ComfyUI (SDXL), txt2img
  - edit_image(image, prompt, ...)  -> Bild via ComfyUI, img2img
  - inpaint_image(image, mask, ...) -> Bild via ComfyUI, maskiertes Inpainting

Bildgenerierung läuft NICHT mehr über diffusers im eigenen Prozess (das lieferte
auf dieser GPU/ROCm-Version gelegentlich NaN-Latents -> Schwarzbilder), sondern
wird an einen laufenden ComfyUI-Server (127.0.0.1:8188) delegiert. ComfyUI ist
auf dieser GPU zuverlässig (kein torch/diffusers-Import mehr im media-mcp-
Prozess für Bilder -> weniger VRAM/RAM, schnellerer Start).

VRAM-schonend: Whisper wird lazy geladen und nach IDLE_UNLOAD_S Sekunden
Leerlauf wieder freigegeben (damit große LLMs in Ollama die 24 GB zurück-
bekommen). _ensure_vram() entlädt vor jedem GPU-Job (Whisper ODER ComfyUI)
etwaige residente Ollama-Modelle (Time-Sharing).

chezmoi-managed: ~/.local/share/chezmoi/ai/media-mcp/executable_server.py
Start als OpenRC-Service (media-mcp) oder: ~/ai/media-mcp/run.sh
"""
import os, sys, time, uuid, threading, subprocess, datetime, pathlib, json, base64, signal, re
import urllib.request, urllib.error, urllib.parse

HOME = pathlib.Path.home()
AI = HOME / "ai"
INCOMING = AI / "incoming"          # hier abgelegte Dateien per Kurzname erreichbar
UPLOADS = AI / "odysseus" / "data" / "uploads"   # Odysseus-Chat-Uploads (verschachtelt nach Datum)
OUTPUTS = AI / "media-mcp" / "outputs"
MEDIA_TMP = AI / "media-mcp" / "tmp"   # transiente Arbeitsdateien (Video-Keyframes) — NICHT unter INCOMING (sonst rglob-Verschmutzung)
# Odysseus serviert Bilder unter /api/generated-image/<file>; der Dateiname MUSS
# ^[a-f0-9]{8,64}\.(png|...) matchen (src/generated_images.py). Wir legen jedes
# erzeugte/bearbeitete Bild zusätzlich hier ab, damit es inline im Chat rendert.
GENERATED_IMAGES = AI / "odysseus" / "data" / "generated_images"
DOWNLOADS = HOME / "Downloads"      # Hermes-Desktop haengt Dateien oft direkt aus ~/Downloads an
# Hermes-Desktop staged Chat-Anhaenge in <cwd>/.hermes/desktop-attachments/. Die
# beobachtete cwd ist ~/ai/media-mcp/ (media-mcp-Serverdir); die kanonische waere
# ~/.hermes/. Beide erlauben, damit im Desktop angehaengte mp4/Audio direkt
# transkribiert werden koennen (sonst 'ausserhalb erlaubter Verzeichnisse').
HERMES_ATTACH = AI / "media-mcp" / ".hermes" / "desktop-attachments"
HERMES_ATTACH_HOME = HOME / ".hermes" / "desktop-attachments"
IDLE_UNLOAD_S = int(os.environ.get("MEDIA_MCP_IDLE_UNLOAD_S", "180"))
PORT = int(os.environ.get("MEDIA_MCP_PORT", "8765"))
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
# SearXNG-Backend fuer deep_research. Default: LOKAL (:8888, Wohn-/LTE-IP). Die
# Datacenter-IP des vServers wird von Suchmaschinen aggressiv ge-CAPTCHA-t/rate-
# limited -> nur bing antwortet, und mit Muell; die Wohn-IP bekommt lokal
# startpage/ddg zuverlaessig (empirisch: lokal 29 relevante Treffer vs vServer 10
# Muell fuer dieselbe Query). vServer bleibt als Fallback verdrahtet:
# SEARXNG_URL=https://search.joshuahirsig.xyz (+ Basic-Auth via ~/ai/.searxng_auth).
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
SEARXNG_AUTH_FILE = AI / ".searxng_auth"   # 'user:pass' fuers vServer-SearXNG (nur bei https; NICHT in git)
# Piper-TTS: CPU-only ONNX (keine GPU/VRAM, kein Wettstreit mit Ollama/ComfyUI).
PIPER_VOICES_DIR = AI / "piper-voices"
PIPER_VOICES = {"de": "de_DE-thorsten-high.onnx", "en": "en_US-lessac-medium.onnx"}
# Vision-Modell (Bild/Video-Analyse) — qwen2.5vl:7b (~6.5GB GPU, zuverlaessig ROCm).
VLM_MODEL = os.environ.get("VLM_MODEL", "qwen2.5vl:7b")

COMFY_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
COMFY_LAUNCH_SCRIPT = str(AI / "comfyui.sh")
COMFY_CKPT = "Juggernaut-XL_v9.safetensors"   # fotoreal SDXL-Finetune (sd_xl_base_1.0 machte abstrakte Farbwirbel)
# Baseline-Negativ, wenn der Aufrufer keinen mitgibt: unterdrückt genau die
# abstrakten/übersättigten Fehlbilder, die das nackte SDXL-Base gern erzeugt.
_DEFAULT_NEG = ("blurry, low quality, low resolution, distorted, deformed, "
                "abstract, surreal, oversaturated, painting, sketch, cartoon, "
                "watermark, text, signature, jpeg artifacts")
# Style -> Checkpoint + optimale Sampler-Settings. Die KI waehlt den Style beim
# Tool-Call: "realistic" (Foto/Produkt/Portrait), "anime" (Anime/Manga),
# "artistic" (Gemaelde/Comic/Fantasy/Illustration). Jedes Modell hat andere
# optimale cfg/sampler. VORWAERTSKOMPATIBEL: ein "flux"-Style waere nur ein
# weiterer Eintrag (dann mit eigenem Workflow, da FLUX andere Nodes braucht).
STYLES = {
    "realistic": {"ckpt": "Juggernaut-XL_v9.safetensors", "cfg": 5.0, "sampler": "dpmpp_2m",        "scheduler": "karras",
                  "neg": _DEFAULT_NEG},
    # Animagine 4.0 ist Danbooru-getunt: braucht Quality-Tags im Positive, sonst
    # liefert es unvollstaendig (z.B. Szene ohne Figur). Und ein ANIME-taugliches
    # Negativ -- NICHT das realistic-Negativ (das "cartoon/anime/painting" enthaelt
    # und damit genau den Anime-Look unterdruecken wuerde).
    "anime":     {"ckpt": "animagine-xl-4.0.safetensors", "cfg": 6.0, "sampler": "euler_ancestral", "scheduler": "normal",
                  "pos": "masterpiece, best quality, very aesthetic, absurdres, highly detailed",
                  "neg": "lowres, worst quality, low quality, bad anatomy, bad hands, missing fingers, "
                         "extra digits, fewer digits, cropped, jpeg artifacts, signature, watermark, "
                         "username, blurry, artist name"},
    # Playground/artistic: schlankes Negativ OHNE "painting/sketch" (die den
    # gewollten malerischen Look killen wuerden).
    "artistic":  {"ckpt": "playground-v2.5.safetensors",  "cfg": 3.0, "sampler": "dpmpp_2m",        "scheduler": "karras",
                  "neg": "lowres, worst quality, low quality, blurry, jpeg artifacts, watermark, "
                         "signature, deformed, disfigured, extra limbs"},
    # FLUX.1-dev (all-in-one fp8): beste Qualitaet + LESBARER Text (SDXL kann keine
    # Schrift). Tickt anders: cfg=1 (kein klassisches Negativ), braucht den
    # FluxGuidance-Node (guidance ~3.5), euler/simple, ~20 Steps. ~17 GB -> laedt
    # wenn _ensure_vram Ollama entladen hat. Lizenz: nicht-kommerziell (privat ok).
    # "flux": True -> generate_image nimmt den FLUX-Workflow (_wf_flux) statt SDXL.
    "flux":      {"ckpt": "flux1-dev-fp8.safetensors", "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "steps": 20, "guidance": 3.5, "flux": True},
    # Qwen-Image (20B): BESTER Text (deutsche Umlaute, lange Woerter, mehrsprachig)
    # + starke Allround-Qualitaet. Eigener Workflow (_wf_qwen): UNETLoader +
    # CLIPLoader(qwen_image) + VAELoader (Split-Files in diffusion_models/,
    # text_encoders/, vae/). ComfyUI swappt Encoder<->Diffusion -> passt in 24 GB,
    # aber LANGSAMER. shift 1.15 bringt das Modell selbst mit (kein extra Node).
    "qwen-image": {"ckpt": "qwen_image_fp8_e4m3fn.safetensors",
                   "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                   "vae": "qwen_image_vae.safetensors",
                   "cfg": 2.5, "sampler": "euler", "scheduler": "simple", "steps": 20, "qwen": True},
}
DEFAULT_STYLE = "realistic"


def _style(style):
    """Style-Name -> Settings-Dict. Unbekannter Style -> realistic. Fehlt die
    Modelldatei noch (Download laeuft), ebenfalls Fallback auf realistic, damit
    nie ein harter Fehler wegen fehlendem File auftritt. Qwen-Image liegt in
    diffusion_models/ (Split-Format), die anderen in checkpoints/."""
    models = AI / "ComfyUI" / "models"
    s = STYLES.get((style or "").strip().lower(), STYLES[DEFAULT_STYLE])
    main_dir = models / ("diffusion_models" if s.get("qwen") else "checkpoints")
    if not (main_dir / s["ckpt"]).exists():
        fb = STYLES[DEFAULT_STYLE]
        if (models / "checkpoints" / fb["ckpt"]).exists():
            sys.stderr.write(f"[media-mcp] Style-Modell {s['ckpt']} fehlt -> Fallback realistic\n")
            return fb
    return s


def _style_names() -> str:
    return ", ".join(STYLES.keys())


# Text/Schrift-Indikatoren im Prompt (DE+EN). Anfuehrungszeichen um ein Wort ODER
# ein Schluesselwort -> der Nutzer will lesbare Schrift im Bild.
_TEXT_HINT_RE = re.compile(
    r'["“”„‘’«»]'
    r'|\b(text|schriftzug|beschriftung|aufschrift|schrift|lettering|logo|'
    r'sign|schild|banner|caption|slogan|titel|title|slogan|wort|wörter|'
    r'words?|writing|typograph\w*|typografie|label|says|reads|spelling|'
    r'geschrieben|steht\s+drauf|drauf\s+steht)\b',
    re.IGNORECASE)


def _auto_style(prompt: str, style: str) -> str:
    """Server-seitiges Style-Routing als Netz gegen die unzuverlaessige Style-Wahl
    des lokalen LLMs im wichtigsten Fall: verlangt der Prompt lesbaren Text/Schrift
    und hat der Aufrufer KEINEN bewussten Nicht-Standard-Style gesetzt -> auf
    'flux' hochstufen (SDXL kann keine Schrift, FLUX schon). Eine explizite
    anime/artistic/flux-Wahl bleibt unangetastet."""
    if (style or "").strip().lower() in ("", "realistic", "auto", "default"):
        if _TEXT_HINT_RE.search(prompt or "") and "flux" in STYLES:
            ckpt = AI / "ComfyUI" / "models" / "checkpoints" / STYLES["flux"]["ckpt"]
            if ckpt.exists():
                sys.stderr.write("[media-mcp] Text/Schrift im Prompt erkannt -> Auto-Style: flux\n")
                return "flux"
    return style


def _pos_neg(prompt: str, negative_prompt: str, s: dict):
    """Effektiver Positive/Negative-Text pro Style: haengt den Style-Quality-Suffix
    (s['pos'], z.B. Animagine-Tags) an den Prompt und nutzt das Style-eigene Negativ
    (s['neg']) statt des realistic-Defaults -- ausser der Aufrufer gibt ein eigenes
    negative_prompt an (dann gewinnt seins)."""
    pos = f"{prompt}, {s['pos']}" if s.get("pos") else prompt
    neg = negative_prompt or s.get("neg") or _DEFAULT_NEG
    return pos, neg


COMFY_START_TIMEOUT_S = 120

INCOMING.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)
MEDIA_TMP.mkdir(parents=True, exist_ok=True)

# torch/transformers erst bei Bedarf importieren (schneller Start, weniger VRAM).
# Für Bilder wird KEIN torch/diffusers mehr im media-mcp-Prozess geladen -> ComfyUI
# läuft als eigener Prozess und übernimmt die GPU-Arbeit.
_lock = threading.Lock()
_state = {"whisper": {}, "last_use": 0.0}


def _touch():
    _state["last_use"] = time.time()


def _ollama_unload():
    """Entlädt alle in Ollama residenten Modelle (keep_alive=0) -> VRAM frei.
    Ollama lädt sie beim nächsten Chat-Request automatisch von NVMe nach."""
    try:
        ps = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5))
        for m in ps.get("models", []):
            body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
            req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            sys.stderr.write(f"[media-mcp] Ollama-Modell entladen: {m['name']}\n")
    except Exception as e:
        sys.stderr.write(f"[media-mcp] Ollama-Unload übersprungen: {e}\n")


def _ensure_vram(need_bytes=None):
    """Gibt VRAM für einen GPU-Job frei per Time-Sharing: ist in Ollama ein Modell
    resident, wird es entladen (lädt beim nächsten Chat automatisch nach).
    HINWEIS: torch.cuda.mem_get_info() ist auf ROCm UNZUVERLÄSSIG (zählt GTT/
    System-RAM mit -> meldet freies VRAM zu hoch). Daher fragen wir Ollama direkt."""
    try:
        ps = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5))
        models = [m.get("name") for m in ps.get("models", [])]
    except Exception as e:
        sys.stderr.write(f"[media-mcp] Ollama /api/ps nicht erreichbar: {e}\n")
        sys.stderr.flush()
        return
    if models:
        sys.stderr.write(f"[media-mcp] GPU-Job: entlade residente Ollama-Modelle "
                         f"{models} (laden beim nächsten Chat nach)\n"); sys.stderr.flush()
        _ollama_unload()
        time.sleep(2)                       # VRAM-Freigabe abwarten
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def _idle_reaper():
    """Gibt geladene Whisper-Modelle nach Leerlauf frei -> VRAM zurück an Ollama.
    ComfyUI läuft als eigener Prozess und verwaltet sein VRAM selbst."""
    while True:
        time.sleep(20)
        with _lock:
            if _state["last_use"] and (time.time() - _state["last_use"] > IDLE_UNLOAD_S):
                if (_state["whisper"] or _state.get("diarizer") or _state.get("spkrec")
                        or _chatterbox_cache):
                    _state["whisper"].clear()
                    _state["diarizer"] = None
                    _state["spkrec"] = None
                    _chatterbox_cache.clear()
                    try:
                        import torch, gc
                        gc.collect()            # pyannote-Pipeline haelt Ref-Zyklen -> erst gc,
                        torch.cuda.empty_cache()  # dann Cache leeren -> deterministische VRAM-Freigabe
                    except Exception:
                        pass
                    _state["last_use"] = 0.0
                    sys.stderr.write("[media-mcp] idle -> Whisper/Diarizer entladen, VRAM frei\n")


def _within_roots(real: pathlib.Path, roots) -> bool:
    for root in roots:
        try:
            root_real = root.resolve()
        except Exception:
            continue
        try:
            real.relative_to(root_real)
            return True
        except ValueError:
            continue
    return False


def _resolve_file(file: str) -> str:
    """Akzeptiert absoluten Pfad, file://-URI oder Kurzname in ~/ai/incoming/.
    SICHERHEIT: jeder Kandidat wird via .resolve() aufgelöst (folgt Symlinks,
    normalisiert '..') und MUSS danach innerhalb von INCOMING oder OUTPUTS
    liegen - sonst FileNotFoundError. Verhindert Path-Traversal, absolute
    Fremdpfade (/etc/passwd) und Symlink-Escapes."""
    if file.startswith("file://"):
        file = file[7:]
    roots = (INCOMING, OUTPUTS)
    p = pathlib.Path(os.path.expanduser(file))
    candidates = [p, INCOMING / file, INCOMING / pathlib.Path(file).name]
    for cand in candidates:
        try:
            if not cand.exists():
                continue
            real = cand.resolve()
            if not real.is_file():
                continue
            if _within_roots(real, roots):
                return str(real)
        except Exception:
            continue
    raise FileNotFoundError(
        f"Datei nicht gefunden oder außerhalb erlaubter Verzeichnisse ({INCOMING}, {OUTPUTS}): "
        f"{file}. Absoluten Pfad innerhalb dieser Verzeichnisse angeben oder Datei dorthin legen.")


def _resolve_media_file(file: str) -> str:
    """Wie _resolve_file, sucht aber zusätzlich rekursiv nach dem Basename in
    ~/ai/incoming/ und den Odysseus-Chat-Uploads (~/ai/odysseus/data/uploads/,
    dort verschachtelt nach Jahr/Monat/Tag). Für edit_image/inpaint_image, deren
    Quellbilder typischerweise aus dem Chat-Upload stammen.

    SICHERHEIT: jeder Kandidat wird via .resolve() aufgelöst und MUSS danach
    innerhalb von INCOMING, UPLOADS oder OUTPUTS liegen. Fällt die Basename-
    Suche in diesen Wurzeln leer aus, wird als Fallback uploads.json (Odysseus-
    Attachment-Bridge) nach name/original_name durchsucht -- gilt aber
    weiterhin nur für Dateien, die letztlich innerhalb der erlaubten Wurzeln
    liegen."""
    if file.startswith("file://"):
        file = file[7:]
    roots = (INCOMING, UPLOADS, OUTPUTS, GENERATED_IMAGES, DOWNLOADS,
             HERMES_ATTACH, HERMES_ATTACH_HOME)
    name = pathlib.Path(file).name
    if not name:
        raise FileNotFoundError(f"Leerer Dateiname: {file!r}")

    p = pathlib.Path(os.path.expanduser(file))
    direct_candidates = [p, INCOMING / file, UPLOADS / file, GENERATED_IMAGES / file]
    for cand in direct_candidates:
        try:
            if not cand.exists():
                continue
            real = cand.resolve()
            if real.is_file() and _within_roots(real, roots):
                return str(real)
        except Exception:
            continue

    # Rekursive Basename-Suche in den erlaubten Wurzeln -> neuestes Match (mtime) gewinnt.
    hits = []
    for base in (INCOMING, UPLOADS, OUTPUTS, GENERATED_IMAGES):
        if not base.exists():
            continue
        for hit in base.rglob("*"):
            try:
                if hit.is_file() and hit.name == name:
                    real = hit.resolve()
                    if _within_roots(real, roots):
                        hits.append(real)
            except Exception:
                continue
    if hits:
        hits.sort(key=lambda h: h.stat().st_mtime, reverse=True)
        return str(hits[0])

    # Fallback: Odysseus-Attachment-Bridge (Chat-Upload-Manifest uploads.json).
    bridged = _resolve_via_uploads_json(file, name, roots)
    if bridged:
        return bridged

    raise FileNotFoundError(
        f"Datei nicht gefunden oder außerhalb erlaubter Verzeichnisse ({INCOMING}, {UPLOADS}, "
        f"{OUTPUTS}): {file} (gesucht nach Dateiname '{name}'). Absoluten Pfad angeben oder "
        f"Datei dorthin legen. Falls das Bild in diesem Chat angehängt wurde, die id= aus dem "
        f"'Uploaded files attached to the latest user turn'-Manifest verwenden, nicht den "
        f"Original-Dateinamen.")


UPLOADS_MANIFEST = AI / "odysseus" / "data" / "uploads" / "uploads.json"


def _resolve_via_uploads_json(file: str, name: str, roots):
    """Odysseus-Attachment-Bridge: schlägt die Basename-Suche fehl, wird
    uploads.json (Dict von Upload-Zeilen) nach 'name'/'original_name' == file
    ODER == name durchsucht. Die neueste passende Zeile (uploaded_at/
    last_accessed) liefert per Basename von id/path den on-disk-Pfad unter
    UPLOADS. Bleibt an den Containment-Guard gebunden."""
    try:
        rows = json.loads(UPLOADS_MANIFEST.read_text())
    except Exception:
        return None
    if not isinstance(rows, dict):
        return None

    matches = []
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        if row.get("name") == file or row.get("original_name") == file \
                or row.get("name") == name or row.get("original_name") == name:
            matches.append(row)
    if not matches:
        return None

    def sort_key(row):
        return row.get("uploaded_at") or row.get("last_accessed") or ""
    matches.sort(key=sort_key, reverse=True)

    for row in matches:
        basename = pathlib.Path(row.get("id") or row.get("path") or "").name
        if not basename:
            continue
        if UPLOADS.exists():
            for hit in UPLOADS.rglob(basename):
                try:
                    if hit.is_file():
                        real = hit.resolve()
                        if _within_roots(real, roots):
                            return str(real)
                except Exception:
                    continue
    return None


def _to_wav(src: str) -> str:
    """Extrahiert/normalisiert Audio nach 16kHz-mono-wav (ffmpeg). Auch mp4/mkv/…"""
    out = OUTPUTS / (pathlib.Path(src).stem + "_16k.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out)],
            check=True, capture_output=True, timeout=600)
    except subprocess.CalledProcessError as e:
        try:
            pathlib.Path(out).unlink()   # Teil-Ausgabe nach ffmpeg-Abbruch nicht in OUTPUTS liegen lassen
        except Exception:
            pass
        tail = e.stderr.decode(errors="replace")[-500:] if e.stderr else "(kein stderr)"
        raise RuntimeError(f"ffmpeg fehlgeschlagen (rc={e.returncode}) für {src}: ...{tail}") from e
    return str(out)


def _whisper(model_id: str):
    import torch
    from transformers import pipeline
    if model_id not in _state["whisper"]:
        _state["whisper"][model_id] = pipeline(
            "automatic-speech-recognition", model=model_id,
            torch_dtype=torch.float16, device="cuda")
    return _state["whisper"][model_id]


def _diarizer():
    """Lazy-laedt die pyannote community-1 Sprecher-Diarization-Pipeline (gecacht
    in _state, auf GPU). HF-Token aus ~/ai/.hf_token oder HF_TOKEN. torchcodec
    wird NICHT gebraucht -> wir fuettern pyannote ein vorgeladenes Waveform."""
    if not _state.get("diarizer"):
        import torch
        from pyannote.audio import Pipeline
        tok = os.environ.get("HF_TOKEN", "").strip()
        tokfile = AI / ".hf_token"
        if not tok and tokfile.exists():
            tok = tokfile.read_text().strip()
        if not tok:
            raise RuntimeError("Kein HF-Token (~/ai/.hf_token oder HF_TOKEN) fuer pyannote-Diarization")
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=tok)
        pipe.to(torch.device("cuda"))
        _state["diarizer"] = pipe
        sys.stderr.write("[media-mcp] pyannote community-1 geladen (GPU)\n")
    return _state["diarizer"]


def _diarize_wav(wav_path: str):
    """Fuehrt Sprecher-Diarization auf einer WAV-Datei aus. Laedt das Audio ueber
    soundfile (nicht torchcodec) und uebergibt das Waveform direkt -> vermeidet
    den kaputten torchcodec-Decoder. Rueckgabe: Liste (start, end, speaker)."""
    import torch, soundfile as sf
    data, sr = sf.read(wav_path, dtype="float32")
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)          # mono
    wf = torch.from_numpy(data).unsqueeze(0)   # (channel=1, time)
    out = _diarizer()({"waveform": wf, "sample_rate": int(sr)})
    # pyannote.audio 4.x: __call__ liefert ein DiarizeOutput; die eigentliche
    # Annotation (mit itertracks) steckt in .speaker_diarization.
    ann = getattr(out, "speaker_diarization", out)
    return [(seg.start, seg.end, spk) for seg, _, spk in ann.itertracks(yield_label=True)]


def _merge_diarization(chunks, turns, name_map: dict = None) -> str:
    """Verbindet Whisper-Zeitstempel-Chunks mit den pyannote-Sprecher-Turns:
    jedem Chunk wird der Sprecher mit dem groessten zeitlichen Overlap zugewiesen.
    Aufeinanderfolgende Chunks desselben Sprechers werden zusammengefasst."""
    def _spk_for(s, e):
        if e is None or e <= s:
            e = s + 0.1            # Whisper-Endchunk hat oft end=None -> Mini-Intervall
        best, best_ov = None, 0.0
        for ts, te, spk in turns:
            ov = min(e, te) - max(s, ts)
            if ov > best_ov:
                best_ov, best = ov, spk
        if best is None:           # kein Overlap -> Turn, der den Startpunkt enthaelt
            for ts, te, spk in turns:
                if ts <= s <= te:
                    return spk
        return best

    def _fmt(t):                   # MM:SS, ab 1h H:MM:SS
        t = int(t); h = t // 3600
        return f"{h}:{(t % 3600)//60:02d}:{t % 60:02d}" if h else f"{t//60:02d}:{t % 60:02d}"

    lines, cur_spk, cur_txt, cur_start = [], None, [], None
    def _flush():
        if cur_spk is not None and cur_txt:
            label = (name_map or {}).get(cur_spk, cur_spk)
            lines.append(f"[{label} {_fmt(cur_start)}] " + " ".join(cur_txt).strip())
    for c in chunks:
        ts = c.get("timestamp") or (None, None)
        s, e = ts[0], ts[1]
        txt = (c.get("text") or "").strip()
        if s is None or not txt:
            continue
        spk = _spk_for(s, e) or "SPEAKER_?"
        if spk != cur_spk:
            _flush()
            cur_spk, cur_txt, cur_start = spk, [txt], s
        else:
            cur_txt.append(txt)
    _flush()
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Sprecher-Wiedererkennung (optional): speechbrain ECAPA ordnet SPEAKER_XX
# echten Namen zu, wenn die Stimme vorher per enroll_speaker() registriert
# wurde. Rein additiv zur gemma-Namenserkennung aus dem Gespraechskontext -
# degradiert IMMER graceful (kein speechbrain, kein Profil, kein Match ueber
# SPK_MATCH_THRESHOLD -> bleibt bei SPEAKER_XX, nie ein Fehler nach oben).
# --------------------------------------------------------------------------
SPEAKER_PROFILES_DIR = AI / "speaker-profiles"
SPK_MATCH_THRESHOLD = 0.25    # roher Cosine-Cutoff fuer spkrec-ecapa-voxceleb
SPK_MATCH_MARGIN = 0.05       # bester vs zweitbester zu nah -> mehrdeutig, kein Auto-Match
_SPK_ID_MAX_S = 20.0          # Deckel je Sprecher (mehr Audio bringt kaum was)
_SPK_ID_MIN_S = 1.0           # darunter unzuverlaessig -> ueberspringen
_SPK_ID_MIN_SEG_S = 0.3       # Diarization-Jitter-Kruemel ignorieren


def _speaker_id_model():
    """Lazy-laedt speechbrain ECAPA (gecacht in _state, GPU). Wirft ImportError,
    wenn speechbrain fehlt -> Aufrufer faengt das ab und bleibt bei SPEAKER_XX."""
    if not _state.get("spkrec"):
        from speechbrain.inference import EncoderClassifier
        _state["spkrec"] = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(AI / ".speechbrain" / "spkrec-ecapa-voxceleb"),
            run_opts={"device": "cuda"})
        sys.stderr.write("[media-mcp] speechbrain ECAPA geladen (GPU)\n")
    return _state["spkrec"]


def _spk_slug(name: str) -> str:
    s = re.sub(r"[^\w-]+", "_", name.strip(), flags=re.UNICODE).strip("_").lower()
    return s or f"speaker-{uuid.uuid4().hex[:8]}"


def _load_profiles() -> dict:
    """slug -> {name, embedding(np 192,)} aus ~/ai/speaker-profiles/*.json."""
    import numpy as np
    profiles = {}
    if not SPEAKER_PROFILES_DIR.exists():
        return profiles
    for p in SPEAKER_PROFILES_DIR.glob("*.json"):
        try:
            row = json.loads(p.read_text())
            profiles[p.stem] = {"name": row["name"],
                                "embedding": np.array(row["embedding"], dtype=np.float32)}
        except Exception as e:
            sys.stderr.write(f"[media-mcp] Sprecher-Profil {p} unlesbar: {e}\n")
    return profiles


def _save_profile(name: str, new_emb) -> int:
    """Neues Sample einmitteln (gewichteter Mittelwert, L2-normalisiert) oder neu
    anlegen. Mehrfach-enroll fuer dieselbe Person verbessert statt ueberschreibt."""
    import numpy as np
    SPEAKER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    new_emb = np.asarray(new_emb, dtype=np.float32)
    new_emb = new_emb / (np.linalg.norm(new_emb) + 1e-9)
    path = SPEAKER_PROFILES_DIR / f"{_spk_slug(name)}.json"
    n = 1
    if path.exists():
        try:
            row = json.loads(path.read_text())
            old = np.array(row["embedding"], dtype=np.float32)
            n = row.get("n_samples", 1)
            combined = (old * n + new_emb) / (n + 1)
            new_emb = combined / (np.linalg.norm(combined) + 1e-9)
            n += 1
        except Exception:
            n = 1
    path.write_text(json.dumps({"name": name.strip(), "embedding": new_emb.tolist(),
                                "n_samples": n,
                                "updated": datetime.datetime.now().isoformat()}, indent=2))
    return n


def _extract_speaker_audio(wav_path: str, turns, spk_label: str):
    """Konkateniert die Segmente EINES Sprechers (chronologisch, gedeckelt bei
    _SPK_ID_MAX_S). None, wenn zu wenig Material fuer eine verlaessliche Kennung."""
    import soundfile as sf, numpy as np
    data, file_sr = sf.read(wav_path, dtype="float32")
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    segs, total = [], 0.0
    for ts, te, spk in sorted(turns, key=lambda t: t[0]):
        if spk != spk_label or (te - ts) < _SPK_ID_MIN_SEG_S or total >= _SPK_ID_MAX_S:
            continue
        s, e = int(ts * file_sr), int(min(te, ts + (_SPK_ID_MAX_S - total)) * file_sr)
        if e > s:
            segs.append(data[s:e]); total += (e - s) / file_sr
    if total < _SPK_ID_MIN_S:
        return None
    return np.concatenate(segs)


def _embed_audio(model, wav_array):
    """ECAPA-Embedding (192,) fuer ein 16kHz-mono-Waveform-Array."""
    import torch
    wf = torch.from_numpy(wav_array).unsqueeze(0)
    emb = model.encode_batch(wf, normalize=False)
    return emb.squeeze().detach().cpu().numpy()


def _match_speaker(emb, profiles: dict):
    """(name, score) des besten sicheren Profils, sonst (None, best_score)."""
    import numpy as np
    if not profiles:
        return None, 0.0
    emb = emb / (np.linalg.norm(emb) + 1e-9)
    scored = sorted(((p["name"], float(np.dot(emb, p["embedding"]))) for p in profiles.values()),
                    key=lambda x: x[1], reverse=True)
    best_name, best_score = scored[0]
    if best_score < SPK_MATCH_THRESHOLD:
        return None, best_score
    if len(scored) > 1 and (best_score - scored[1][1]) < SPK_MATCH_MARGIN:
        sys.stderr.write(f"[media-mcp] Sprecher-Match mehrdeutig: {scored[:2]} -> SPEAKER_XX\n")
        return None, best_score
    return best_name, best_score


def _identify_speakers(wav_path: str, turns) -> dict:
    """SPEAKER_XX -> echter Name, wo ein enrolltes Profil sicher matcht. Faellt bei
    fehlendem speechbrain / fehlenden Profilen / jedem Fehler graceful auf {} zurueck
    (wirft NIE nach oben durch)."""
    try:
        profiles = _load_profiles()
        if not profiles:
            return {}
        model = _speaker_id_model()
        name_map = {}
        for spk in {t[2] for t in turns}:
            audio = _extract_speaker_audio(wav_path, turns, spk)
            if audio is None:
                continue
            name, score = _match_speaker(_embed_audio(model, audio), profiles)
            sys.stderr.write(f"[media-mcp] Sprecher-Erkennung {spk}: "
                             f"{name or '(kein Match)'} (score={score:.3f})\n")
            if name:
                name_map[spk] = name
        return name_map
    except ImportError:
        sys.stderr.write("[media-mcp] speechbrain nicht installiert -> Sprecher-Erkennung "
                         "uebersprungen\n")
        return {}
    except Exception as e:
        sys.stderr.write(f"[media-mcp] Sprecher-Erkennung fehlgeschlagen: {type(e).__name__}: {e}\n")
        return {}


# --------------------------------------------------------------------------
# ComfyUI-Backend (ersetzt diffusers/SDXL im eigenen Prozess)
# --------------------------------------------------------------------------

def _comfy_alive() -> bool:
    try:
        urllib.request.urlopen(f"{COMFY_URL}/system_stats", timeout=3).read()
        return True
    except Exception:
        return False


def _comfy_ensure_running():
    """Prüft, ob ComfyUI erreichbar ist; startet es sonst detached und wartet,
    bis die API antwortet (Modell-Load kann initial dauern)."""
    if _comfy_alive():
        return
    log_path = OUTPUTS / "comfyui-launch.log"
    sys.stderr.write(f"[media-mcp] ComfyUI nicht erreichbar -> starte {COMFY_LAUNCH_SCRIPT} "
                     f"(Log: {log_path})\n")
    sys.stderr.flush()
    env = dict(os.environ)
    env["HSA_TOOLS_LIB"] = ""            # roctracer-Assertion vermeiden
    env.setdefault("MIOPEN_USER_DB_PATH", str(AI / ".miopen"))
    # stderr/stdout in ein Log statt DEVNULL -> Startfehler bleiben diagnostizierbar.
    logf = open(log_path, "ab", buffering=0)
    logf.write(f"\n===== ComfyUI-Launch {datetime.datetime.now().isoformat()} =====\n".encode())
    proc = subprocess.Popen(
        [COMFY_LAUNCH_SCRIPT], env=env, cwd=str(AI),
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.time() + COMFY_START_TIMEOUT_S
    while time.time() < deadline:
        if _comfy_alive():
            sys.stderr.write("[media-mcp] ComfyUI ist bereit\n"); sys.stderr.flush()
            return
        rc = proc.poll()
        if rc is not None:
            raise RuntimeError(
                f"ComfyUI-Launcher beendete sich vorzeitig (rc={rc}), API nie erreichbar. "
                f"Details im Log: {log_path}")
        time.sleep(1.5)
    raise RuntimeError(f"ComfyUI startete nicht innerhalb von {COMFY_START_TIMEOUT_S}s "
                       f"(Log: {log_path})")


def _comfy_upload_image(path: str) -> dict:
    """Lädt ein Bild in ComfyUIs input/-Ordner hoch (multipart/form-data).
    Rückgabe: {name, subfolder, type} zum Referenzieren in LoadImage/LoadImageMask."""
    p = pathlib.Path(path)
    data = p.read_bytes()
    # Multipart-Filename bereinigen: ", CR und LF könnten sonst den Header
    # aufbrechen (Header-Injection) bzw. den Multipart-Frame zerlegen.
    safe_name = p.name.replace('"', "").replace("\r", "").replace("\n", "") or "image.png"
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    return resp


def _comfy_submit(workflow: dict) -> str:
    client_id = str(uuid.uuid4())
    body = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"ComfyUI /prompt Fehler {e.code}: {detail}")
    if "prompt_id" not in resp:
        raise RuntimeError(f"ComfyUI /prompt ohne prompt_id: {resp}")
    return resp["prompt_id"]


def _comfy_cancel(prompt_id: str):
    """Bricht einen laufenden/queued Job ab: /interrupt stoppt die aktuelle
    Ausführung, /queue {delete:[id]} entfernt ihn aus der Warteschlange.
    Verhindert Zombie-Jobs, die den nächsten Aufruf vergiften."""
    for url, body in ((f"{COMFY_URL}/interrupt", {}),
                      (f"{COMFY_URL}/queue", {"delete": [prompt_id]})):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            sys.stderr.write(f"[media-mcp] ComfyUI-Cancel ({url}) fehlgeschlagen: {e}\n")


def _comfy_wait_result(prompt_id: str, timeout_s: float = 300.0) -> list:
    """Pollt /history/{prompt_id}, bis Outputs vorliegen. Gibt die Liste der
    Bild-Deskriptoren [{filename, subfolder, type}, ...] des SaveImage-Nodes zurück.

    Robustheit: zählt aufeinanderfolgende Verbindungsfehler -> nach ~5 gilt
    ComfyUI als tot (localhost refused) und wir brechen ab, statt still zu
    warten. Bei Timeout wird der Job aktiv abgebrochen (kein Zombie)."""
    deadline = time.time() + timeout_s
    conn_fail = 0
    while time.time() < deadline:
        try:
            hist = json.load(urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10))
            conn_fail = 0
        except Exception as e:
            conn_fail += 1
            if conn_fail >= 5:
                _comfy_cancel(prompt_id)
                raise RuntimeError(
                    f"ComfyUI /history {conn_fail}x nicht erreichbar -> Prozess vermutlich "
                    f"tot (letzter Fehler: {e})")
            hist = {}
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI-Job fehlgeschlagen: {status}")
            outputs = entry.get("outputs", {})
            images = []
            for node_out in outputs.values():
                images.extend(node_out.get("images", []))
            if images:
                return images
            if status.get("completed"):
                raise RuntimeError(f"ComfyUI-Job fertig, aber keine Bilder in Outputs: {outputs}")
        time.sleep(1.0)
    _comfy_cancel(prompt_id)
    raise TimeoutError(f"ComfyUI-Job {prompt_id} lieferte nach {timeout_s}s kein Ergebnis "
                       f"(Job abgebrochen)")


def _comfy_fetch_image(desc: dict) -> bytes:
    q = urllib.parse.urlencode({
        "filename": desc["filename"], "subfolder": desc.get("subfolder", ""),
        "type": desc.get("type", "output")})
    return urllib.request.urlopen(f"{COMFY_URL}/view?{q}", timeout=30).read()


def _wf_txt2img(prompt: str, negative_prompt: str, steps: int, width: int, height: int,
                 seed: int, style: str = DEFAULT_STYLE) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    s = _style(style)
    pos, neg = _pos_neg(prompt, negative_prompt, s)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": s["ckpt"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height),
                                                            "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed_val, "steps": (s.get("steps") or int(steps)), "cfg": s["cfg"],
            "sampler_name": s["sampler"], "scheduler": s["scheduler"], "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_img2img(uploaded: dict, prompt: str, negative_prompt: str, strength: float,
                 steps: int, seed: int, tw: int, th: int, style: str = DEFAULT_STYLE) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    img_ref = uploaded["name"]
    s = _style(style)
    pos, neg = _pos_neg(prompt, negative_prompt, s)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": s["ckpt"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
        "8": {"class_type": "LoadImage", "inputs": {"image": img_ref}},
        # Defect-1-Fix: auf SDXL-Auflösung (~1024 lange Seite) hochskalieren, BEVOR
        # VAEEncode -> sonst 'color bomb' bei kleinen Eingaben.
        "11": {"class_type": "ImageScale", "inputs": {
            "image": ["8", 0], "upscale_method": "lanczos",
            "width": int(tw), "height": int(th), "crop": "disabled"}},
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["1", 2]}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["9", 0],
            "seed": seed_val, "steps": (s.get("steps") or int(steps)), "cfg": s["cfg"],
            "sampler_name": s["sampler"], "scheduler": s["scheduler"], "denoise": float(strength)}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_inpaint(uploaded_image: dict, uploaded_mask: dict, prompt: str, negative_prompt: str,
                 steps: int, seed: int, tw: int, th: int, style: str = DEFAULT_STYLE) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    s = _style(style)
    pos, neg = _pos_neg(prompt, negative_prompt, s)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": s["ckpt"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
        # Defect-1-Fix: Bild UND Maske auf dieselbe SDXL-Zielauflösung (tw x th)
        # skalieren -> Maße stimmen exakt überein (VAEEncodeForInpaint verlangt
        # das) und das Sampling läuft bei ~1024px statt 'color bomb'. Die Maske
        # wird als IMAGE geladen, mitskaliert und dann via ImageToMask (red)
        # in eine MASK gewandelt.
        "8": {"class_type": "LoadImage", "inputs": {"image": uploaded_image["name"]}},
        "11": {"class_type": "ImageScale", "inputs": {
            "image": ["8", 0], "upscale_method": "lanczos",
            "width": int(tw), "height": int(th), "crop": "disabled"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": uploaded_mask["name"]}},
        "13": {"class_type": "ImageScale", "inputs": {
            "image": ["12", 0], "upscale_method": "bilinear",
            "width": int(tw), "height": int(th), "crop": "disabled"}},
        "14": {"class_type": "ImageToMask", "inputs": {"image": ["13", 0], "channel": "red"}},
        "9": {"class_type": "VAEEncodeForInpaint", "inputs": {
            "pixels": ["11", 0], "vae": ["1", 2], "mask": ["14", 0], "grow_mask_by": 6}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["9", 0],
            "seed": seed_val, "steps": (s.get("steps") or int(steps)), "cfg": s["cfg"],
            "sampler_name": s["sampler"], "scheduler": s["scheduler"], "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_flux(prompt: str, steps: int, width: int, height: int, seed: int,
             style: str = "flux") -> dict:
    """txt2img-Workflow für FLUX (all-in-one fp8). FLUX tickt anders als SDXL:
    braucht den FluxGuidance-Node (dev ist guidance-distilled, guidance ~3.5),
    läuft mit cfg=1 (kein klassisches Negativ). Das positive Conditioning geht
    durch FluxGuidance, bevor es in den KSampler geht. Steps/Guidance kommen aus
    dem Style-Eintrag."""
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    s = _style(style)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": s["ckpt"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "10": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["2", 0],
                                                         "guidance": s.get("guidance", 3.5)}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height),
                                                            "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["10", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed_val, "steps": (s.get("steps") or int(steps)), "cfg": s.get("cfg", 1.0),
            "sampler_name": s.get("sampler", "euler"), "scheduler": s.get("scheduler", "simple"),
            "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_qwen(prompt: str, negative_prompt: str, steps: int, width: int, height: int,
             seed: int, style: str = "qwen-image") -> dict:
    """txt2img-Workflow für Qwen-Image (Split-Format). Anders als SDXL/FLUX-
    All-in-One: getrennter UNETLoader (Diffusion), CLIPLoader mit type
    'qwen_image' (Qwen2.5-VL Text-Encoder) und VAELoader. 16-Kanal-Latent ->
    EmptySD3LatentImage. Den Sampling-Shift (1.15) bringt das Modell selbst mit."""
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    s = _style(style)
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": s["ckpt"], "weight_dtype": "default"}},
        "20": {"class_type": "CLIPLoader", "inputs": {"clip_name": s["clip"], "type": "qwen_image"}},
        "21": {"class_type": "VAELoader", "inputs": {"vae_name": s["vae"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["20", 0]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt or "", "clip": ["20", 0]}},
        "4": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(width), "height": int(height),
                                                              "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed_val, "steps": (s.get("steps") or int(steps)), "cfg": s.get("cfg", 2.5),
            "sampler_name": s.get("sampler", "euler"), "scheduler": s.get("scheduler", "simple"),
            "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["21", 0]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_txt2video(prompt: str, negative_prompt: str, width: int, height: int, length: int,
                  steps: int, fps: int, seed: int) -> dict:
    """txt2video-Workflow für LTX-Video 0.9.8 (distilled). Distilled tickt wie ein
    Turbo-Modell: cfg=1.0, ~8 Steps, euler. Der Text-Encoder ist ein SEPARATER
    CLIPLoader (t5xxl, type 'ltxv'), NICHT im Checkpoint. Das Sampling läuft über
    SamplerCustom mit von LTXVScheduler erzeugten Sigmas (kein klassischer KSampler).
    LTXVConditioning hat ZWEI Ausgänge — 0=positive, 1=negative — daher referenziert
    Node 9 ["6",0] UND ["6",1]. Output als mp4 via VHS_VideoCombine (VideoHelperSuite).
    # nicht live-verifiziert: ComfyUI war beim Bau nicht erreichbar (GET /object_info
    # HTTP 000); Node-/Input-Namen gegen die aktuellen ComfyUI-LTXV-Nodes gesetzt."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "ltxv-13b-0.9.8-distilled-fp8.safetensors"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "t5xxl_fp8_e4m3fn_scaled.safetensors", "type": "ltxv"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative_prompt}},
        "5": {"class_type": "EmptyLTXVLatentVideo",
              "inputs": {"width": int(width), "height": int(height), "length": int(length),
                         "batch_size": 1}},
        "6": {"class_type": "LTXVConditioning",
              "inputs": {"positive": ["3", 0], "negative": ["4", 0], "frame_rate": int(fps)}},
        "7": {"class_type": "LTXVScheduler",
              "inputs": {"steps": int(steps), "max_shift": 2.05, "base_shift": 0.95,
                         "stretch": True, "terminal": 0.1, "latent": ["5", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "SamplerCustom",
              "inputs": {"add_noise": True, "noise_seed": int(seed), "cfg": 1.0,
                         "model": ["1", 0], "positive": ["6", 0], "negative": ["6", 1],
                         "sampler": ["8", 0], "sigmas": ["7", 0], "latent_image": ["5", 0]}},
        "10": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["9", 0], "vae": ["1", 2],
               "tile_size": 256, "overlap": 32, "temporal_size": 32, "temporal_overlap": 4}},
        "11": {"class_type": "VHS_VideoCombine",
               "inputs": {"images": ["10", 0], "frame_rate": int(fps), "loop_count": 0,
                          "filename_prefix": "media-mcp-vid", "format": "video/h264-mp4",
                          "pingpong": False, "save_output": True}},
    }


def _wf_music(tags: str, lyrics: str, seconds: float, steps: int, seed: int) -> dict:
    """txt2audio-Workflow für ACE-Step 1.5 (turbo). Aus dem ComfyUI-Blueprint
    'Text to Audio (ACE-Step 1.5).json' ins API-Format übersetzt (der Blueprint ist
    ein Subgraph ohne Save-Node -> hier um SaveAudio ergänzt). Kette:
    UNETLoader -> ModelSamplingAuraFlow(shift 3); DualCLIPLoader(type 'ace',
    qwen_0.6b + qwen_4b) -> TextEncodeAceStepAudio1.5 (tags/lyrics/duration); dessen
    Conditioning geht als positive in den KSampler und via ConditioningZeroOut als
    negative. EmptyAceStep1.5LatentAudio(seconds) -> KSampler(8 Steps, cfg 1,
    euler/simple) -> VAEDecodeAudio -> SaveAudio (flac). Widget-Defaults (bpm 190,
    timesignature '4', language 'en', keyscale 'E minor', cfg_scale 2, temperature
    0.85, top_p 0.9) 1:1 aus dem Blueprint.
    # nicht live-verifiziert: ComfyUI war beim Bau nicht erreichbar (GET /object_info
    # HTTP 000); Node-/Input-Namen aus dem Blueprint übernommen."""
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "acestep_v1.5_turbo.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "3": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": "qwen_0.6b_ace15.safetensors",
                         "clip_name2": "qwen_4b_ace15.safetensors",
                         "type": "ace", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "ace_1.5_vae.safetensors"}},
        "5": {"class_type": "TextEncodeAceStepAudio1.5",
              "inputs": {"clip": ["3", 0], "tags": tags, "lyrics": lyrics,
                         "seed": int(seed), "bpm": 190, "duration": float(seconds),
                         "timesignature": "4", "language": "en", "keyscale": "E minor",
                         "generate_audio_codes": True, "cfg_scale": 2.0,
                         "temperature": 0.85, "top_p": 0.9, "top_k": 0, "min_p": 0.0}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptyAceStep1.5LatentAudio",
              "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["2", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "seed": int(seed), "steps": int(steps),
                         "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "9": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["4", 0]}},
        "10": {"class_type": "SaveAudio",
               "inputs": {"audio": ["9", 0], "filename_prefix": "media-mcp-music"}},
    }


def _comfy_free_vram():
    """Entlädt ComfyUIs Modelle (POST /free) -> gibt VRAM sofort an Ollama zurück.
    WICHTIG: ComfyUI hält SDXL sonst mit ~7GB im Leerlauf und verdrängt das große
    LLM in Ollama in den System-RAM (CPU-Speed). Der nächste Bild-Job lädt SDXL
    kalt neu (~10s) — bewusst, damit der Chat schnell bleibt (Chat > Bilder)."""
    try:
        req = urllib.request.Request(
            f"{COMFY_URL}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        sys.stderr.write("[media-mcp] ComfyUI-VRAM freigegeben (SDXL entladen)\n")
    except Exception as e:
        sys.stderr.write(f"[media-mcp] ComfyUI /free übersprungen: {e}\n")


def _comfy_stop():
    """Beendet den ComfyUI-Prozess KOMPLETT (nicht nur /free).

    ComfyUIs Torch-Caching-Allocator haelt nach einem Bildjob ~11 GB VRAM idle
    fest, die POST /free NICHT zurueckgibt (es entlaedt nur das Modell, nicht den
    Allocator-Cache). Solange ComfyUI laeuft, passt das grosse LLM daher nicht
    ganz auf die GPU -> Ollama lagert ~44 % in den System-RAM aus (CPU-Speed).
    Nur ein Prozess-Exit gibt den VRAM WIRKLICH frei; danach laeuft qwen3:30b zu
    100 % auf der GPU. _comfy_ensure_running startet ComfyUI beim naechsten
    Bildjob kalt neu (~10-20s) -> bewusst, damit der Chat schnell bleibt.

    Der Launcher (comfyui.sh) startet via ``exec python main.py --listen
    127.0.0.1 --port 8188`` in eigener Session (Popen start_new_session=True),
    daher ist der Prozess ueber sein cmdline eindeutig findbar und per
    Prozessgruppe sauber beendbar."""
    if not _comfy_alive():
        return
    _comfy_free_vram()   # zuerst graceful die Modelle entladen
    try:
        out = subprocess.run(
            ["pgrep", "-f", "main.py --listen 127.0.0.1 --port 8188"],
            capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    except Exception as e:
        sys.stderr.write(f"[media-mcp] ComfyUI-PID-Suche fehlgeschlagen: {e}\n")
        return

    def _signal_all(sig):
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), sig)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(pid, sig)
                except Exception:
                    pass

    _signal_all(signal.SIGTERM)
    for _ in range(24):            # bis zu ~12s auf sauberen Exit warten
        if not _comfy_alive():
            sys.stderr.write("[media-mcp] ComfyUI gestoppt -> VRAM voll frei fuer LLM\n")
            return
        time.sleep(0.5)
    _signal_all(signal.SIGKILL)   # haengt noch -> hart nachlegen
    sys.stderr.write("[media-mcp] ComfyUI mit SIGKILL beendet\n")


def _comfy_run(workflow: dict, prompt: str, timeout_s: float = 300.0) -> pathlib.Path:
    """Submitted einen Workflow, wartet auf das Ergebnis, speichert es unter
    ~/ai/media-mcp/outputs/img-<ts>.png und gibt den Pfad zurück."""
    prompt_id = _comfy_submit(workflow)
    try:
        images = _comfy_wait_result(prompt_id, timeout_s=timeout_s)
        data = _comfy_fetch_image(images[0])
    finally:
        _comfy_stop()               # ComfyUI ganz beenden -> VRAM (auch Torch-Cache)
                                    # voll zurück an Ollama, auch bei Fehler
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = OUTPUTS / f"img-{ts}.png"
    out.write_bytes(data)
    return out


def _comfy_wait_output(prompt_id: str, output_key: str, timeout_s: float = 600.0) -> list:
    """Wie _comfy_wait_result, aber erntet einen beliebigen Output-Key statt fest
    'images' — z.B. 'gifs' (VHS_VideoCombine meldet seine mp4 dort) oder 'audio'
    (SaveAudio). Gleiche {filename, subfolder, type}-Deskriptor-Form, gleiche
    Robustheit (Connection-Fail-Zähler, aktiver Abbruch bei Timeout)."""
    deadline = time.time() + timeout_s
    conn_fail = 0
    while time.time() < deadline:
        try:
            hist = json.load(urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10))
            conn_fail = 0
        except Exception as e:
            conn_fail += 1
            if conn_fail >= 5:
                _comfy_cancel(prompt_id)
                raise RuntimeError(
                    f"ComfyUI /history {conn_fail}x nicht erreichbar -> Prozess vermutlich "
                    f"tot (letzter Fehler: {e})")
            hist = {}
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI-Job fehlgeschlagen: {status}")
            outputs = entry.get("outputs", {})
            found = []
            for node_out in outputs.values():
                found.extend(node_out.get(output_key, []))
            if found:
                return found
            if status.get("completed"):
                raise RuntimeError(
                    f"ComfyUI-Job fertig, aber kein '{output_key}'-Output: {outputs}")
        time.sleep(1.0)
    _comfy_cancel(prompt_id)
    raise TimeoutError(f"ComfyUI-Job {prompt_id} lieferte nach {timeout_s}s kein Ergebnis "
                       f"(Job abgebrochen)")


def _comfy_run_video(workflow: dict, timeout_s: float = 600.0) -> pathlib.Path:
    """Wie _comfy_run, aber für Video: erntet den 'gifs'-Output-Key (dort meldet
    VHS_VideoCombine die mp4) und speichert nach outputs/vid-<ts>-<uuid>.mp4.
    _comfy_stop() im finally -> VRAM (auch Torch-Cache) zurück an Ollama, auch bei
    Fehler."""
    prompt_id = _comfy_submit(workflow)
    try:
        vids = _comfy_wait_output(prompt_id, "gifs", timeout_s=timeout_s)
        data = _comfy_fetch_image(vids[0])
    finally:
        _comfy_stop()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = OUTPUTS / f"vid-{ts}-{uuid.uuid4().hex[:8]}.mp4"
    out.write_bytes(data)
    return out


def _comfy_run_audio(workflow: dict, timeout_s: float = 600.0) -> pathlib.Path:
    """Wie _comfy_run, aber für Audio: erntet den 'audio'-Output-Key (SaveAudio) und
    speichert nach outputs/music-<ts>-<uuid>.<ext>; die Endung (flac) kommt aus dem
    von ComfyUI zurückgemeldeten Dateinamen. _comfy_stop() im finally."""
    prompt_id = _comfy_submit(workflow)
    try:
        auds = _comfy_wait_output(prompt_id, "audio", timeout_s=timeout_s)
        desc = auds[0]
        data = _comfy_fetch_image(desc)
    finally:
        _comfy_stop()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    ext = pathlib.Path(desc.get("filename", "")).suffix.lstrip(".") or "flac"
    out = OUTPUTS / f"music-{ts}-{uuid.uuid4().hex[:8]}.{ext}"
    out.write_bytes(data)
    return out


def _clamp_dim(v: int) -> int:
    """Auflösung auf gültiges SDXL-Raster begrenzen: Vielfaches von 8, 256..2048."""
    return max(256, min(2048, (int(v) // 8) * 8))


def _sdxl_target_dims(src: str, long_side: int = 1024) -> tuple:
    """Zielauflösung (w, h) für SDXL-img2img/inpaint: die LANGE Seite wird auf
    ~long_side px skaliert, Seitenverhältnis erhalten, beide Maße Vielfache von 8.

    HINTERGRUND (Defect 1): SDXL ist auf ~1024px trainiert. Ein kleines Bild
    (z.B. 296x336) direkt via LoadImage->VAEEncode in SDXL-img2img zu geben,
    liefert inkohärenten, übersättigten Müll ('color bomb'). Vor dem VAEEncode
    muss deshalb hochskaliert und bei der Upscale-Auflösung gesampelt werden."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            w, h = im.size
    except Exception as e:
        sys.stderr.write(f"[media-mcp] Bildmaße nicht lesbar ({src}): {e} -> {long_side}^2\n")
        return long_side, long_side
    if w <= 0 or h <= 0:
        return long_side, long_side
    if w >= h:
        tw = long_side
        th = max(8, round(long_side * h / w / 8) * 8)
    else:
        th = long_side
        tw = max(8, round(long_side * w / h / 8) * 8)
    return int(tw), int(th)


def _clamp_steps(v: int) -> int:
    return max(1, min(60, int(v)))


def _pick_seed(seed: int) -> int:
    """Konkreten Seed bestimmen: <0 -> zufällig (32-bit), sonst der übergebene.
    So kann der tatsächlich verwendete Seed in der Caption berichtet werden."""
    return int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF


def _comfy_timeout_for(steps: int) -> float:
    """Timeout skaliert mit der Step-Zahl und deckt den Cold-Start (Modell-Load,
    MIOpen-Kernel-Autotune) mit ab."""
    return max(300, int(steps) * 8)


from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent
mcp = FastMCP("battlestation-media", host="0.0.0.0", port=PORT)


# --------------------------------------------------------------------------
# Visuelle Sprecher-Erkennung (optional): liest das sichtbare Namens-Label des
# aktiv markierten Sprechers aus Zoom/Teams/Meet-Aufnahmen via qwen2.5vl -> echte
# Namen OHNE Voice-Enrollment. Kombiniert mit der stimmlichen Erkennung
# (_identify_speakers). Degradiert IMMER graceful auf {} (kein Video, kein
# lesbares Label, kein Konsens -> SPEAKER_XX bleibt, nie ein Fehler nach oben).
# --------------------------------------------------------------------------
_VIS_MIN_TURN_S = 1.5
_VIS_MIN_FRAMES_TOTAL = 2
_VIS_MIN_VOTES = 2
_VIS_FRAME_BUDGET = 48    # ~max VLM-Calls gesamt, ueber die Sprecher aufgeteilt
_VIS_FRAMES_MIN = 5       # min Frames/Sprecher (Zoom-Galerie, viele Teilnehmer)
_VIS_FRAMES_MAX = 16      # max Frames/Sprecher (wenige Sprecher -> dichter, faengt kurze Bauchbinden)
# True: ein EINZIGER unwidersprochener Namens-Read reicht (faengt kurz eingeblendete
# TV-Bauchbinden, aber auf mieser Videoqualitaet anfaellig fuer Fehllesungen).
# False: nur >= _VIS_MIN_VOTES uebereinstimmende Reads (sicherer, verpasst kurze Einblendungen).
_VIS_ACCEPT_SINGLE = True

VLM_SPEAKER_PROMPT = (
    "This is a frame from a video: either a video call (Zoom, Microsoft Teams, Google Meet) or a "
    "TV broadcast / presentation. Identify the person who is the CURRENT ACTIVE SPEAKER or main "
    "presenter: in a video call it is the participant tile highlighted with a colored border or "
    "shown large; in a broadcast it is the main person on screen. Read their NAME if it appears on "
    "screen — either as the tile's name label OR as a caption / lower-third / name banner. Respond "
    "with ONLY that name, exactly as written on screen, nothing else — no explanation, no quotes. "
    "If no name is visible anywhere on screen, or you are unsure, respond with exactly: NONE"
)


def _has_video_stream(path: str) -> bool:
    """True nur bei ECHTEM Videostream — NICHT bei eingebettetem Cover-Bild: mp3/m4a/
    flac Album-Art ist ein 'attached_pic'-Video-Stream. Ohne diesen Ausschluss liefe
    die visuelle Sprecher-Erkennung auf reinem Podcast-Audio (dutzende VLM-Calls auf
    dasselbe Cover) und koennte Sprecher faelschlich nach dem Cover-Text benennen."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        for line in out.stdout.splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) >= 2 and f[0] == "video" and f[1] == "0":   # echt, kein attached_pic
                return True
        return False
    except Exception:
        return False


def _pick_speaker_frame_times(turns, spk, n):
    """n Zeitpunkte fuer spk, GLEICHMAESSIG ueber seine GESAMTE Redezeit verteilt —
    samplet INNERHALB der Turns (nicht nur an Mittelpunkten). So werden auch bei nur
    wenigen langen Turns (TV/Praesentation) genug ueber die Zeit gestreute Frames
    erzeugt, um eine nur kurz eingeblendete Bauchbinde/ein Namensschild zu erwischen."""
    ivals = sorted((ts, te) for ts, te, s in turns if s == spk and te > ts)
    total = sum(te - ts for ts, te in ivals)
    if not ivals or total < _VIS_MIN_TURN_S:   # zu wenig Redezeit (Diarization-Jitter) -> ueberspringen
        return []
    n = max(1, int(n))
    times = []
    for i in range(n):
        off = total * (i + 0.5) / n          # gleichmaessig ueber die konkatenierte Redezeit
        acc = 0.0
        for ts, te in ivals:                 # Offset auf echte Videozeit zurueckmappen
            d = te - ts
            if off < acc + d:
                times.append(round(ts + (off - acc), 2))
                break
            acc += d
    return sorted(set(times))


def _normalize_name(raw):
    s = (raw or "").strip().strip('"\'').strip()
    if not s or s.upper() == "NONE" or len(s) > 60 or "\n" in s:
        return ""
    return re.sub(r"\s+", " ", s)


def _majority_name(names):
    from collections import Counter
    valid = [n for n in (_normalize_name(n) for n in names) if n]
    if not valid:
        return ""
    counts = Counter(n.casefold() for n in valid)
    best_key, best_n = counts.most_common(1)[0]
    # Regel 1: >= _VIS_MIN_VOTES Frames lesen denselben Namen -> sicher (Zoom-Galerie,
    # Sprecher dauerhaft mit Namen sichtbar).
    if best_n >= _VIS_MIN_VOTES:
        return next(n for n in valid if n.casefold() == best_key)
    # Regel 2: genau EIN einziger Name ueber alle Frames (unwidersprochen, Rest NONE)
    # -> akzeptieren WENN _VIS_ACCEPT_SINGLE. Deckt kurz eingeblendete TV-Bauchbinden
    # ab; auf mieser Qualitaet aber fehleranfaellig (kann falschen Namen liefern).
    if _VIS_ACCEPT_SINGLE and len(counts) == 1:
        return valid[0]
    return ""


def _identify_speakers_visual(video, turns):
    """SPEAKER_XX -> Name aus dem sichtbaren Label des aktiv markierten Sprechers
    (Zoom/Teams/Meet). Degradiert IMMER graceful auf {} (kein Videostream, kein
    Frame, Ollama weg, kein Konsens) -- wirft NIE nach oben durch, Transkript+
    Diarization sind hier schon fertig."""
    import tempfile, shutil
    if not turns or not _has_video_stream(video):
        return {}
    speakers = {t[2] for t in turns}
    # Frame-Budget ueber die Sprecher aufteilen: wenige Sprecher (TV/Praesentation)
    # -> dichter (faengt kurze Bauchbinden); viele (Zoom-Galerie) -> je 5.
    n_per = max(_VIS_FRAMES_MIN, min(_VIS_FRAMES_MAX, round(_VIS_FRAME_BUDGET / max(1, len(speakers)))))
    spk_times = {}
    for spk in speakers:
        times = _pick_speaker_frame_times(turns, spk, n_per)
        if len(times) >= _VIS_MIN_FRAMES_TOTAL:
            spk_times[spk] = times
    if not spk_times:
        return {}
    tmp = tempfile.mkdtemp(prefix="vis-spk-", dir=str(MEDIA_TMP))
    try:
        _free_media_vram()            # Whisper/pyannote/ECAPA raus -> Platz fuer VLM
        _ensure_vram(8_000_000_000)   # residentes Ollama-Chat-Modell entladen
        jobs = [(spk, i, t) for spk, times in spk_times.items() for i, t in enumerate(times)]
        votes = {spk: [] for spk in spk_times}
        for call_i, (spk, i, t) in enumerate(jobs, 1):
            frame = os.path.join(tmp, f"{spk}_{i}.jpg")
            raw = ""
            try:
                subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video,
                                "-frames:v", "1", "-q:v", "2", frame],
                               check=True, capture_output=True, timeout=30)
                data = pathlib.Path(frame).read_bytes()
                ka = "0" if call_i == len(jobs) else "2m"
                raw = _vlm_caption(data, VLM_SPEAKER_PROMPT, keep_alive=ka)
            except Exception as e:
                sys.stderr.write(f"[media-mcp] visuelle Sprechererkennung {spk}@{t:.1f}s: {type(e).__name__}: {e}\n")
            votes[spk].append(raw)
            _touch()
        name_map = {}
        for spk, v in votes.items():
            name = _majority_name(v)
            sys.stderr.write(f"[media-mcp] visuelle Sprechererkennung {spk}: {name or '(kein Konsens)'} (roh={v})\n")
            if name:
                name_map[spk] = name
        return name_map
    except Exception as e:
        sys.stderr.write(f"[media-mcp] visuelle Sprechererkennung fehlgeschlagen: {type(e).__name__}: {e}\n")
        return {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _identify_speakers_from_text(diarized: str, model: str = "gemma4:26b") -> dict:
    """PRIO 1: SPEAKER_XX -> echter Name aus dem GESPRAECHSKONTEXT (Selbstvorstellung,
    Anrede mit Namen, Begruessung). gemma liest das diarisierte Transkript. Kein Video/
    Enrollment noetig -> greift bei jedem Meeting, in dem man sich mit Namen anspricht.
    Graceful {} bei Unklarheit/Fehler (wirft nie nach oben durch)."""
    labels = sorted(set(re.findall(r"\[(SPEAKER_\d+)", diarized)))
    if not labels:
        return {}
    prompt = (
        "Hier ist ein diarisiertes Meeting-Transkript mit anonymen Sprecher-Labels "
        f"({', '.join(labels)}). Bestimme fuer JEDES Label den ECHTEN Namen der Person, "
        "soweit er sich aus dem Gespraech EINDEUTIG ergibt: Selbstvorstellung ('ich bin X', "
        "'hier spricht X'), direkte Anrede ('danke X', 'X, was meinst du'), Begruessung, "
        "Verabschiedung. Gib NUR ein JSON-Objekt zurueck, das Labels auf Namen abbildet, und "
        "NUR fuer Labels mit WIRKLICH eindeutigem Namen -- unsichere weglassen. Erfinde KEINE "
        "Namen. Beispiel: {\"SPEAKER_00\": \"Anna Meier\", \"SPEAKER_02\": \"Kai\"}. Ist kein "
        "Name sicher, gib {} zurueck.\n\n---\n" + diarized[:60000] + "\n---")
    try:
        _ensure_vram(18_000_000_000)   # gemma ~17GB
        raw = _ollama_generate(model, prompt, num_ctx=32768)
        # An jeder '{'-Position raw_decode versuchen (erstes gueltiges Objekt gewinnt)
        # -> robust, falls gemma das Beispiel-Objekt UND seine Antwort ausgibt (greedy
        # {.*} wuerde beide + Prosa dazwischen fassen und json.loads scheitern lassen).
        data = None
        dec = json.JSONDecoder()
        for mm in re.finditer(r"\{", raw):
            try:
                data, _ = dec.raw_decode(raw[mm.start():])
                break
            except Exception:
                continue
        if not isinstance(data, dict):
            return {}
        labelset = set(labels)   # nur echte, im Transkript vorkommende Labels
        return {k: v.strip() for k, v in data.items()
                if str(k) in labelset and isinstance(v, str) and v.strip()}
    except Exception as e:
        sys.stderr.write(f"[media-mcp] Kontext-Namenserkennung: {type(e).__name__}: {e}\n")
        return {}


def _transcribe_file(file: str, language: str = "auto", diarize: bool = False, use_video: bool = True) -> str:
    """Kern der Transkription — OHNE _lock (der Aufrufer haelt ihn). Von transcribe
    UND summarize_meeting genutzt, damit es keinen Lock-Deadlock gibt."""
    _touch()
    src = _resolve_media_file(file)
    wav = _to_wav(src)
    try:
        lang = language.lower().strip()
        if lang in ("swiss", "gsw", "ch", "schweizerdeutsch"):
            model_id = "Flurin17/whisper-large-v3-turbo-swiss-german"
            gen = {"language": "de"}
        else:
            model_id = "openai/whisper-large-v3-turbo"
            gen = {} if lang in ("", "auto") else {"language": lang}
        _ensure_vram(6_000_000_000 if diarize else 5_000_000_000)  # Whisper (+pyannote)
        asr = _whisper(model_id)
        # batch_size=8 + large-v3-TURBO: _ensure_vram entlaedt Ollama VOR dem Job
        # -> ~22GB frei, kein HIP-Crash (die alte batch=2-Grenze galt nur mit
        # residentem LLM). turbo ~6-8x schneller -> 40-Min-Video in ~4 Min statt >1h.
        res = asr(wav, chunk_length_s=30, batch_size=8, generate_kwargs=gen,
                  return_timestamps=bool(diarize))
        _touch()
        text = (res.get("text") or "").strip()
        if diarize:
            try:
                turns = _diarize_wav(wav)
                _touch()
                if not turns:
                    sys.stderr.write("[media-mcp] Diarization: keine Sprecher erkannt -> nur Transkript\n")
                    return text or "(leeres Transkript)"
                # Roher SPEAKER_XX-Text zuerst -> Basis fuer die Kontext-Namenserkennung.
                base = _merge_diarization(res.get("chunks", []), turns)
                if not base.strip():
                    sys.stderr.write("[media-mcp] Diarization-Merge leer -> nur Transkript\n")
                    return text or "(leeres Transkript)"
                # Namen aus 3 Quellen. Prio: Kontext (1) > visuell (2) > Stimme (3).
                voice_map = _identify_speakers(wav, turns)             # Prio 3 (Enrollment)
                _touch()
                visual_map = {}
                if use_video:
                    try:
                        visual_map = _identify_speakers_visual(src, turns)   # Prio 2 (Namensschild)
                    except Exception as e:
                        sys.stderr.write(f"[media-mcp] visuelle Sprechererkennung uebersprungen: {type(e).__name__}: {e}\n")
                    _touch()
                _free_media_vram()   # Whisper/pyannote/ECAPA raus VOR gemma (17GB) -> kein RAM-Spill
                context_map = _identify_speakers_from_text(base)       # Prio 1 (Gespraechskontext)
                _touch()
                name_map = {}
                name_map.update(voice_map)      # Basis
                name_map.update(visual_map)     # ueberschreibt Stimme
                name_map.update(context_map)    # ueberschreibt alles (Prio 1)
                for spk in sorted(set(voice_map) | set(visual_map) | set(context_map)):
                    parts = []
                    if spk in context_map: parts.append(f"Kontext={context_map[spk]}")
                    if spk in visual_map:  parts.append(f"visuell={visual_map[spk]}")
                    if spk in voice_map:   parts.append(f"Stimme={voice_map[spk]}")
                    sys.stderr.write(f"[media-mcp] {spk} -> {name_map[spk]}  ({'; '.join(parts)})\n")
                merged = _merge_diarization(res.get("chunks", []), turns, name_map)
                n = len({t[2] for t in turns})
                recognized = sorted(set(name_map.values()))
                hdr = f"({n} Sprecher erkannt" + (f", benannt: {', '.join(recognized)})" if recognized else ")")
                return f"{hdr}\n\n{merged}"
            except Exception as e:
                sys.stderr.write(f"[media-mcp] Diarization fehlgeschlagen: {e}\n")
                return (text or "(leeres Transkript)") + \
                       f"\n\n(Hinweis: Sprecher-Trennung fehlgeschlagen: {type(e).__name__} — nur Transkript)"
        return text or "(leeres Transkript)"
    finally:
        try:
            os.unlink(wav)
        except Exception:
            pass


@mcp.tool()
def transcribe(file: str, language: str = "auto", diarize: bool = False, use_video: bool = True) -> str:
    """Transkribiert eine Audio- oder Videodatei (auch .mp4/.mkv/.mov) mit Whisper auf der GPU.

    WICHTIG: EIN Aufruf transkribiert die GANZE Datei — Whisper zerlegt langes
    Audio intern selbst. NICHT das Video in Segmente splitten/ffmpeg/zusammenbauen.
    Auch lange Meetings (30-60 Min) in einem Aufruf; einfach warten.
    Für eine fertige Meeting-ZUSAMMENFASSUNG stattdessen `summarize_meeting` nutzen.

    Args:
        file: Absoluter Pfad (auch ~/Downloads) ODER Dateiname in ~/ai/incoming/.
        language: 'auto' (default, DE/EN-Erkennung), 'de', 'en', oder
                  'swiss'/'gsw' für Schweizerdeutsch (Output = Hochdeutsch).
        diarize: True = zusätzlich Sprecher-Trennung (pyannote). Ausgabe als
                 '[SPEAKER_00 MM:SS] Text' je Sprecherwechsel. Nutzen, wenn der
                 Nutzer 'wer hat was gesagt/beigetragen' oder Sprecher-Zuordnung
                 will. Dauert länger. Labels sind ANONYM (SPEAKER_00/01…) — echte
                 Namen nur, wenn im Gespräch genannt (dann kannst DU sie zuordnen).
        use_video: True (default) = bei Videos mit diarize=True zusätzlich das
                 sichtbare Namens-Label des aktiv markierten Sprechers aus
                 Zoom/Teams/Meet-Aufnahmen lesen (qwen2.5vl) -> echte Namen ohne
                 Voice-Enrollment. Wirkt NUR auf Videos mit diarize=True; bei
                 reinem Audio oder ohne lesbares Label ein harmloser No-op.
    Returns:
        Den transkribierten Text (mit Sprecher-Labels, wenn diarize=True).
    """
    with _lock:
        return _transcribe_file(file, language, diarize, use_video)


@mcp.tool()
def enroll_speaker(name: str, audio: str) -> str:
    """Registriert/verbessert die Stimme einer Person fuer die automatische
    Sprecher-Wiedererkennung: kuenftige transcribe(diarize=True)/summarize_meeting-
    Aufrufe zeigen bei Stimm-Uebereinstimmung den ECHTEN Namen statt SPEAKER_XX.

    OPTIONALE Zusatzschicht zur Diarization — rein additiv. Erneutes Aufrufen mit
    demselben Namen fuegt ein Sample hinzu (mittelt ein, ueberschreibt nicht) ->
    mehrere kurze Samples verbessern die Erkennung. ~10-20s klare Sprache pro Sample.

    Args:
        name: Anzeigename, der kuenftig statt SPEAKER_XX erscheinen soll.
        audio: Pfad/Name einer Audio-/Videodatei, in der NUR diese eine Person
               spricht (keine Fremdstimmen). Suche/Verzeichnisse wie bei transcribe.
    Returns:
        Bestaetigung mit Anzahl der gespeicherten Samples fuer diesen Namen.
    """
    with _lock:
        _touch()
        name = (name or "").strip()
        if not name:
            return "(Fehler: 'name' darf nicht leer sein)"
        src = _resolve_media_file(audio)
        wav = _to_wav(src)
        import soundfile as sf   # ausserhalb des try -> die ImportError-Meldung unten meint eindeutig speechbrain
        try:
            _ensure_vram(1_500_000_000)   # ECAPA ist klein
            model = _speaker_id_model()
            data, sr = sf.read(wav, dtype="float32")
            if getattr(data, "ndim", 1) > 1:
                data = data.mean(axis=1)
            dur = len(data) / sr
            if dur < _SPK_ID_MIN_S:
                return f"(Fehler: Aufnahme zu kurz: {dur:.1f}s < {_SPK_ID_MIN_S}s)"
            data = data[:int(_SPK_ID_MAX_S * sr)]   # deckeln wie die Match-Seite -> kein OOM bei langen Dateien + vergleichbare Embeddings
            n = _save_profile(name, _embed_audio(model, data))
            _touch()
            return f"Stimme fuer '{name}' gespeichert ({n} Sample{'s' if n != 1 else ''}, {min(dur, _SPK_ID_MAX_S):.1f}s genutzt)."
        except ImportError:
            return ("(speechbrain nicht installiert -> Sprecher-Erkennung nicht verfuegbar. "
                    "Installieren: pip install speechbrain -c ~/ai/torch-constraints.txt)")
        except Exception as e:
            sys.stderr.write(f"[media-mcp] enroll_speaker fehlgeschlagen: {e}\n")
            return f"(Enrollment fehlgeschlagen: {type(e).__name__}: {e})"
        finally:
            try:
                os.unlink(wav)
            except Exception:
                pass


def _ollama_generate(model: str, prompt: str, num_ctx: int = 8192,
                     temperature: float = 0.3) -> str:
    """Ein Text-Completion-Call an lokales Ollama (fuer die gemma-Zusammenfassung)."""
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "keep_alive": "60s",
        "options": {"num_ctx": num_ctx, "temperature": temperature},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return (json.load(r).get("response") or "").strip()


def _free_media_vram():
    """Whisper + Diarizer + Sprecher-ID + Chatterbox aus dem Speicher werfen -> VRAM frei fuers gemma-LLM."""
    _state["whisper"].clear()
    _state["diarizer"] = None
    _state["spkrec"] = None
    _chatterbox_cache.clear()
    try:
        import torch, gc
        gc.collect(); torch.cuda.empty_cache()
    except Exception:
        pass


def _chunk_text(text: str, max_chars: int = 14000):
    """Zerlegt den Transkript-Text an Zeilengrenzen (Sprecherwechseln) in Bloecke
    von ~max_chars -> lange Meetings ohne Kontextverlust zusammenfassbar (Map-Reduce)."""
    chunks, cur, cur_len = [], [], 0
    for ln in text.split("\n"):
        if cur and cur_len + len(ln) > max_chars:
            chunks.append("\n".join(cur)); cur, cur_len = [], 0
        cur.append(ln); cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _summarize_transcript(diarized: str, model: str = "gemma4:26b") -> str:
    """Map-Reduce-Zusammenfassung mit gemma: lange Transkripte in Bloecke teilen,
    je Block zusammenfassen (map), dann zur Endfassung verdichten (reduce)."""
    chunks = _chunk_text(diarized)
    if len(chunks) <= 1:
        partials = [diarized]
    else:
        partials = []
        for i, ch in enumerate(chunks, 1):
            p = (f"Dies ist Teil {i}/{len(chunks)} eines Meeting-Transkripts (mit "
                 f"Sprecher-Labels wie [SPEAKER_00]). Fasse NUR diesen Teil sachlich "
                 f"zusammen: welche Sprecher was gesagt/beigetragen haben + Kernpunkte. "
                 f"Erfinde NICHTS. Deutsch.\n\n---\n{ch}\n---")
            partials.append(_ollama_generate(model, p, num_ctx=8192))
            _touch()
    joined = "\n\n".join(partials)
    final_p = (
        "Du bist ein praeziser Protokollant. Aus den folgenden Transkript-"
        "Abschnitten bzw. Teilzusammenfassungen erstelle EINE finale strukturierte "
        "Meeting-Zusammenfassung auf Deutsch:\n"
        "1. **Worum ging es** (2-3 Saetze)\n"
        "2. **Wer hat was beigetragen** (pro Sprecher; wenn im Text Namen genannt "
        "werden, ordne sie den SPEAKER-Labels zu, sonst SPEAKER_XX)\n"
        "3. **Kernpunkte & Entscheidungen**\n"
        "4. **Naechste Schritte / To-dos** (nur falls genannt)\n\n"
        "WICHTIG: NUR Informationen aus dem Text verwenden. Erfinde KEINE Namen, "
        "Zahlen, Termine oder Fakten. Unklares als unklar kennzeichnen.\n\n"
        f"---\n{joined}\n---")
    return _ollama_generate(model, final_p, num_ctx=16384) or "(leere Zusammenfassung)"


@mcp.tool()
def summarize_meeting(file: str, language: str = "auto", diarize: bool = True,
                      use_video: bool = True, model: str = "gemma4:26b") -> str:
    """Transkribiert ein Meeting (Audio/Video) UND liefert eine fertige, strukturierte
    ZUSAMMENFASSUNG (wer hat was beigetragen + Kernpunkte + To-dos).

    Pipeline (alles lokal, GPU): Whisper-Transkript (turbo) -> optional pyannote-
    Sprecher-Trennung -> Map-Reduce-Zusammenfassung mit gemma (lange Meetings werden
    intern in Bloecke geteilt, damit KEIN Kontext verloren geht).

    Nutze DIESES Tool, wenn der Nutzer eine Meeting-Zusammenfassung / 'fasse das
    Meeting zusammen' / 'wer hat was gesagt' will — NICHT transcribe + selbst
    zusammenfassen (das verliert bei langen Meetings Kontext und riskiert Halluzination).

    Args:
        file: Pfad/Name der Audio-/Videodatei (wie bei transcribe).
        language: 'auto'/'de'/'en'/'swiss'.
        diarize: True (default) = Sprecher trennen -> 'wer hat was' moeglich.
        use_video: True (default) = bei Video-Meetings zusaetzlich das sichtbare
                   Namens-Label des aktiven Sprechers aus Zoom/Teams/Meet lesen
                   (qwen2.5vl) -> echte Namen ohne Voice-Enrollment. Nur bei
                   Videos mit diarize=True wirksam, sonst harmloser No-op.
        model: gemma-Modell fuer die Zusammenfassung (default gemma4:26b).
    Returns:
        Eine strukturierte deutsche Zusammenfassung (KEINE erfundenen Fakten).
    """
    with _lock:
        transcript = _transcribe_file(file, language, diarize, use_video)
        if transcript.startswith("(leeres Transkript"):
            return transcript
        _free_media_vram()          # Whisper/pyannote raus -> VRAM frei fuer gemma
        try:
            return _summarize_transcript(transcript, model)
        except Exception as e:
            sys.stderr.write(f"[media-mcp] Zusammenfassung fehlgeschlagen: {e}\n")
            return (f"(Zusammenfassung fehlgeschlagen: {type(e).__name__}. "
                    f"Roh-Transkript:)\n\n{transcript}")


def _summarize_text_content(content: str, model: str, instruction: str = "") -> str:
    """Allgemeine Map-Reduce-Zusammenfassung beliebigen Texts mit gemma."""
    extra = f" Zusatz-Anweisung des Nutzers: {instruction.strip()}" if instruction.strip() else ""
    chunks = _chunk_text(content)
    if len(chunks) <= 1:
        partials = [content]
    else:
        partials = []
        for i, ch in enumerate(chunks, 1):
            p = (f"Teil {i}/{len(chunks)} eines Textes. Fasse NUR diesen Teil sachlich "
                 f"zusammen (nichts erfinden).{extra} Deutsch.\n\n---\n{ch}\n---")
            partials.append(_ollama_generate(model, p, num_ctx=8192))
            _touch()
    joined = "\n\n".join(partials)
    final_p = ("Erstelle aus diesen Abschnitten/Teilzusammenfassungen EINE klare, "
               "strukturierte Zusammenfassung auf Deutsch. NUR Fakten aus dem Text, "
               f"erfinde NICHTS.{extra}\n\n---\n{joined}\n---")
    return _ollama_generate(model, final_p, num_ctx=16384) or "(leere Zusammenfassung)"


@mcp.tool()
def summarize_text(text: str = "", file: str = "", instruction: str = "",
                   model: str = "gemma4:26b") -> str:
    """Fasst beliebigen TEXT oder eine Text-/Markdown-Datei mit gemma zusammen
    (lokale GPU, Map-Reduce fuer lange Texte -> kein Kontextverlust).

    Fuer allgemeine Text-Zusammenfassungen (Artikel, Notizen, Doku). Fuer MEETINGS
    (Audio/Video) stattdessen `summarize_meeting`. gemma ist stark in Prosa-Synthese;
    qwen (das Chat-Modell) muss die eigentliche Zusammenfassung so nicht selbst machen.

    Args:
        text: der zusammenzufassende Text (direkt eingefuegt) — ODER:
        file: Pfad zu einer Text-/Markdown-Datei (nur unterhalb ~/).
        instruction: optionale Zusatz-Anweisung ('als Stichpunkte', 'max 5 Saetze', …).
        model: gemma-Modell (default gemma4:26b).
    Returns:
        Die Zusammenfassung (Deutsch, keine erfundenen Fakten).
    """
    with _lock:
        _touch()
        content = (text or "").strip()
        if not content and file.strip():
            try:
                p = pathlib.Path(os.path.expanduser(file)).resolve()
                if not str(p).startswith(str(HOME)):
                    return "(Fehler: Datei ausserhalb des Home-Verzeichnisses — nicht erlaubt)"
                content = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"(Fehler beim Lesen der Datei: {type(e).__name__})"
        if not content:
            return "(kein Text uebergeben — 'text' oder 'file' angeben)"
        _ensure_vram(18_000_000_000)   # gemma ~17GB -> residentes qwen3 vorher entladen
        try:
            return _summarize_text_content(content, model, instruction)
        except Exception as e:
            sys.stderr.write(f"[media-mcp] summarize_text fehlgeschlagen: {e}\n")
            return f"(Zusammenfassung fehlgeschlagen: {type(e).__name__})"


def _searxng_auth_header() -> dict:
    """Basic-Auth-Header fuers vServer-SearXNG (https, aus ~/ai/.searxng_auth).
    NUR bei https-Instanz -> lokales SearXNG (http) braucht/bekommt keine Auth."""
    if not SEARXNG_URL.startswith("https://"):
        return {}
    try:
        cred = SEARXNG_AUTH_FILE.read_text().strip()
        if cred:
            return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}
    except Exception:
        pass
    return {}


def _searxng_search(query: str, n: int = 6) -> list:
    """SearXNG-JSON-Suche -> [{title, url, content, engine}, ...], nach Score sortiert.
    SEARXNG_URL (default vServer); Basic-Auth via ~/ai/.searxng_auth falls vorhanden."""
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    headers = {"Accept": "application/json", "User-Agent": "media-mcp/deep_research"}
    headers.update(_searxng_auth_header())
    req = urllib.request.Request(f"{SEARXNG_URL}/search?{params}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    # nur dict-Ergebnisse (list.sort ruft den key bei 1 Element NICHT -> sonst
    # koennte ein einzelnes Nicht-dict spaeter an h.get() ungefangen crashen)
    results = [x for x in data.get("results", []) if isinstance(x, dict)]
    results.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    return results[:n]


def _fetch_page_text(url: str, max_chars: int = 9000):
    """Best-effort Klartext-Abruf einer Seite (Script/Style raus, Tags strippen).
    Rueckgabe: Text ODER None bei Fehlschlag/Skip (Caller faellt dann auf den
    SearXNG-Snippet zurueck). Nur http/https -> KEIN file:///ftp-Zugriff (die URLs
    kommen aus SearXNG-Engine-Antworten; ein rogue Engine koennte sonst lokale
    Dateien anfordern)."""
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) media-mcp/deep_research"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype and ctype:
                return None
            raw = r.read(3_000_000)  # max 3 MB/Seite
        html = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"<(script|style|noscript|svg|head)\b.*?</\1>", " ", html,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&(nbsp|amp|quot|#39|lt|gt);", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] or None
    except Exception as e:
        sys.stderr.write(f"[media-mcp] deep_research Abruf {url[:60]}: {type(e).__name__}\n")
        return None


@mcp.tool()
def deep_research(query: str, num_sources: int = 5, model: str = "gemma4:26b") -> str:
    """Echte Multi-Source-Web-Recherche: SearXNG-Suche -> Top-N Quellen ABRUFEN ->
    gemma fasst MIT nummerierten Quellenangaben zusammen. Garantiert MEHRERE Quellen
    (nicht nur die erste) -- das ist der Unterschied zu einzelnen web_search/
    web_extract-Aufrufen, bei denen das Chat-Modell selbst entscheidet (und oft nach
    der 1. Quelle aufhoert).

    NUTZE DIESES TOOL fuer verlaessliche Recherche: News-Ueberblick, Faktencheck,
    Themen-Zusammenfassung, "was ist der aktuelle Stand zu X". Fuer eine einzelne
    bekannte URL reicht web_extract; fuer breite Recherche IMMER deep_research.

    Args:
        query: die Suchanfrage / Forschungsfrage.
        num_sources: wie viele Quellen abgerufen + synthetisiert werden (default 5).
        model: gemma-Modell fuer die Synthese (default gemma4:26b, 256k Kontext).
    Returns:
        Strukturierte Zusammenfassung mit [1]..[N]-Zitaten + Quellenliste.
    """
    num_sources = max(2, min(int(num_sources or 5), 8))
    try:
        hits = _searxng_search(query, n=num_sources)
    except Exception as e:
        return (f"(Recherche fehlgeschlagen: SearXNG ({SEARXNG_URL}) nicht "
                f"erreichbar? {type(e).__name__})")
    if not hits:
        return f"(keine SearXNG-Treffer fuer: {query})"

    # Suche + Seitenabruf laufen OHNE _lock (reines Netz-I/O) -> blockieren keine
    # GPU-Tools. Nur die gemma-Synthese unten braucht Lock + VRAM.
    sources = []
    for i, h in enumerate(hits, 1):
        url = h.get("url", "")
        body = _fetch_page_text(url) or h.get("content") or "(kein Inhalt abrufbar)"
        sources.append(f"[{i}] {h.get('title','')} — {url}\n{body}")

    joined = "\n\n---\n\n".join(sources)
    prompt = (
        "Du bist ein sorgfaeltiger Rechercheur. Fasse die folgenden Quellen zur "
        f"Frage '{query}' zu einem praezisen deutschen Ueberblick zusammen. "
        f"Belege Aussagen mit den Quellennummern [1]-[{len(sources)}] wie unten "
        "nummeriert. Wenn sich Quellen widersprechen, benenne das explizit. "
        "Verwende NUR Informationen aus den Quellen -- erfinde NICHTS, und wenn "
        "die Quellen die Frage nicht beantworten, sage das klar.\n\n"
        f"{joined}")
    with _lock:
        _touch()
        _ensure_vram(18_000_000_000)   # gemma ~17GB -> residentes qwen3 vorher entladen
        try:
            summary = _ollama_generate(model, prompt, num_ctx=32768)
        except Exception as e:
            sys.stderr.write(f"[media-mcp] deep_research-Synthese fehlgeschlagen: {e}\n")
            return f"(Synthese fehlgeschlagen: {type(e).__name__})"
    cites = "\n".join(f"[{i}] {h.get('url','')}" for i, h in enumerate(hits, 1))
    return f"{summary or '(leere Synthese)'}\n\n---\nQuellen:\n{cites}"


def _vlm_caption(image_bytes: bytes, prompt: str, keep_alive: str = "2m") -> str:
    """Ein Vision-Call an lokales Ollama (qwen2.5vl) -> Bildbeschreibung/Antwort.
    keep_alive steuert, ob das Modell resident bleibt (Frame-Schleife) oder danach
    entladen wird ('0' beim letzten Frame -> VRAM frei fuer die gemma-Synthese)."""
    b64 = base64.b64encode(image_bytes).decode()
    body = json.dumps({
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False, "keep_alive": keep_alive,
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return (json.load(r).get("message", {}).get("content") or "").strip()


def _media_duration(path: str) -> float:
    """Videolaenge in Sekunden via ffprobe (0.0 wenn unbekannt)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _extract_keyframes(video: str, tmp: str, max_frames: int = 12) -> list:
    """Extrahiert bis zu max_frames gleichmaessig verteilte Keyframes (JPG) via
    ffmpeg in das (vom Aufrufer angelegte UND aufgeraeumte) Verzeichnis tmp.
    Rueckgabe: sortierte Frame-Pfade."""
    n = max(1, min(int(max_frames or 12), 16))
    dur = _media_duration(video)
    fps = (n / dur) if dur and dur > 0 else 1.0   # ~n Frames ueber die ganze Laenge
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
         "-vf", f"fps={fps:.6f}", "-frames:v", str(n), "-q:v", "3",
         f"{tmp}/frame_%03d.jpg"],
        check=True, capture_output=True, timeout=300)
    return sorted(str(p) for p in pathlib.Path(tmp).glob("*.jpg"))


@mcp.tool()
def describe_image(image: str, question: str = "") -> str:
    """Beschreibt ein Bild oder beantwortet eine konkrete Frage dazu — lokales
    Vision-Modell (qwen2.5vl, GPU). Fuer 'was ist auf dem Bild', Text-Auslesen (OCR),
    Objekt-/Szenen-Fragen.

    Args:
        image: Pfad/Name des Bildes (unter erlaubten Verzeichnissen bzw. Chat-Anhang).
        question: optionale konkrete Frage; leer = allgemeine Beschreibung.
    Returns:
        Die Bildbeschreibung / Antwort (Deutsch).
    """
    with _lock:
        _touch()
        src = _resolve_media_file(image)
        _ensure_vram(8_000_000_000)   # VLM ~6.5GB -> residentes qwen3 vorher entladen
        try:
            data = pathlib.Path(src).read_bytes()
            prompt = question.strip() or "Beschreibe dieses Bild detailliert und sachlich auf Deutsch."
            cap = _vlm_caption(data, prompt, keep_alive="30s")
            _touch()
            return cap or "(keine Beschreibung)"
        except Exception as e:
            sys.stderr.write(f"[media-mcp] describe_image fehlgeschlagen: {e}\n")
            return f"(Bildanalyse fehlgeschlagen: {type(e).__name__}: {e})"


@mcp.tool()
def analyze_video(file: str, question: str = "", max_frames: int = 12) -> str:
    """Analysiert den BILDINHALT eines Videos: extrahiert gleichmaessig verteilte
    Keyframes, laesst jeden vom Vision-Modell (qwen2.5vl, GPU) beschreiben und fasst
    sie mit gemma4 zu einer kohaerenten Gesamtbeschreibung zusammen.

    Fuer 'was passiert im Video', visuelle Zusammenfassung. Fuer GESPROCHENEN Inhalt
    (Meeting/Sprache) stattdessen `summarize_meeting` (Transkript).

    Args:
        file: Pfad/Name des Videos (erlaubte Verzeichnisse bzw. Chat-Anhang).
        question: optionale konkrete Frage; leer = allgemeine Inhalts-Zusammenfassung.
        max_frames: wie viele Keyframes analysiert werden (1-16, default 12).
    Returns:
        Zusammenfassung des visuellen Video-Inhalts (Deutsch).
    """
    with _lock:
        _touch()
        src = _resolve_media_file(file)
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="vid-frames-", dir=str(MEDIA_TMP))
        try:
            try:
                frames = _extract_keyframes(src, tmp, max_frames)
            except Exception as e:
                sys.stderr.write(f"[media-mcp] Keyframe-Extraktion: {e}\n")
                return f"(Keyframe-Extraktion fehlgeschlagen — ffmpeg vorhanden? {type(e).__name__})"
            if not frames:
                return "(keine Frames extrahierbar)"
            _ensure_vram(8_000_000_000)   # VLM ~6.5GB
            caps = []
            for i, fr in enumerate(frames, 1):
                try:
                    data = pathlib.Path(fr).read_bytes()
                    ka = "0" if i == len(frames) else "2m"   # letzter Frame entlaedt VLM -> Platz fuer gemma
                    c = _vlm_caption(data, "Beschreibe kurz und sachlich, was in diesem "
                                           "Videobild zu sehen ist (Personen, Objekte, Handlung, Text). "
                                           "Deutsch, 1-2 Saetze.", keep_alive=ka)
                    caps.append(f"[Frame {i}] {c}")
                except Exception as e:
                    caps.append(f"[Frame {i}] (Fehler: {type(e).__name__})")
                _touch()
            joined = "\n".join(caps)
            q = question.strip() or "Was passiert in diesem Video? Fasse den visuellen Inhalt zusammen."
            prompt = ("Dies sind Beschreibungen zeitlich geordneter Keyframes eines Videos. "
                      f"Beantworte auf ihrer Basis: {q}\nBeschreibe den Ablauf kohaerent auf "
                      "Deutsch. Verwende NUR was in den Frames steht, erfinde nichts.\n\n"
                      f"{joined}")
            _ensure_vram(18_000_000_000)   # gemma ~17GB (VLM via keep_alive:0 schon entladen)
            try:
                summary = _ollama_generate("gemma4:26b", prompt, num_ctx=16384)
            except Exception as e:
                sys.stderr.write(f"[media-mcp] analyze_video-Synthese: {e}\n")
                return f"(Synthese fehlgeschlagen: {type(e).__name__})\n\nEinzelframes:\n{joined}"
            _touch()
            return f"{summary or '(leere Synthese)'}\n\n---\n{len(frames)} Keyframes analysiert."
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


_piper_cache = {}         # voice-key -> geladenes PiperVoice (lazy, CPU, bleibt resident)
_tts_lock = threading.Lock()   # serialisiert Piper/espeak-ng (globaler C-State) gegen sich selbst
_chatterbox_cache = {}    # "model" -> ChatterboxMultilingualTTS (lazy, GPU, bleibt resident)


def _load_chatterbox():
    """Lazy-laedt Chatterbox Multilingual (GPU, ~5-6GB; ~3.1GB Weights beim ersten
    Mal von HF). Gecacht -> nur der erste Klon-Aufruf zahlt Load/Download."""
    if "model" not in _chatterbox_cache:
        sys.stderr.write("[media-mcp] lade Chatterbox Multilingual (GPU)...\n")
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        _chatterbox_cache["model"] = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
    return _chatterbox_cache["model"]


@mcp.tool()
def text_to_speech(text: str, voice: str = "de", reference_audio: str = "",
                   language: str = "de") -> str:
    """Wandelt Text in gesprochene Sprache um (WAV-Datei). Zwei Modi:

    - voice='de'/'en' (Default): Piper, CPU-only (ONNX), kein VRAM, ~10x Echtzeit
      -> laeuft auch waehrend Bild/Transkript die GPU belegen.
    - voice='clone': Chatterbox Multilingual v3 (Resemble AI), GPU-Voice-Cloning
      aus einer Referenzaufnahme (reference_audio, ~5-10s reichen). Offiziell 23
      Sprachen inkl. Deutsch (language). Braucht VRAM -> _ensure_vram entlaedt
      vorher ein laufendes Ollama-Modell (Time-Sharing wie Whisper/ComfyUI).

    Args:
        text: der zu sprechende Text.
        voice: 'de' (Piper/Thorsten), 'en' (Piper/Lessac) oder 'clone' (Chatterbox,
               Stimme aus reference_audio klonen). Default 'de'.
        reference_audio: nur fuer voice='clone' — Pfad/Kurzname einer Referenz-
               Audiodatei (die zu klonende Stimme, ~5-10s sauber gesprochen).
        language: nur fuer voice='clone' — Sprachcode ('de','en','fr',…). Default 'de'.
    Returns:
        Pfad zur erzeugten WAV-Datei (unter ~/ai/media-mcp/outputs/).
    """
    text = (text or "").strip()
    if not text:
        return "(kein Text uebergeben)"
    v = (voice or "de").strip().lower()

    if v == "clone":
        if not reference_audio.strip():
            return "(voice='clone' braucht reference_audio: Pfad zu einer Referenz-Audiodatei)"
        with _lock:                       # GPU-Job -> serialisieren wie transcribe/Bild
            try:
                ref = _resolve_media_file(reference_audio)
                ref_wav = _to_wav(ref)    # -> 16k mono, robust fuer beliebige Eingaben
            except Exception as e:
                return f"(Referenzaudio nicht nutzbar: {type(e).__name__}: {e})"
            try:
                _ensure_vram(6_000_000_000)   # Chatterbox ~5-6GB -> Ollama vorher entladen
                model = _load_chatterbox()
                _touch()
                lraw = (language or "de").strip().lower()
                # Aliase auf ISO-Codes; Chatterbox hat kein Schweizerdeutsch -> de.
                lang = {"gsw": "de", "swiss": "de", "ch": "de", "schweizerdeutsch": "de"}.get(lraw, lraw[:2]) or "de"
                try:                                  # gegen Chatterbox-Sprachliste validieren
                    from chatterbox.mtl_tts import SUPPORTED_LANGUAGES
                    if lang not in SUPPORTED_LANGUAGES:
                        sys.stderr.write(f"[media-mcp] Chatterbox-Sprache '{lang}' unbekannt -> 'de'\n")
                        lang = "de"
                except Exception:
                    pass
                wav = model.generate(text, language_id=lang, audio_prompt_path=ref_wav)
                OUTPUTS.mkdir(parents=True, exist_ok=True)
                out = OUTPUTS / f"tts-clone-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.wav"
                # torchaudio.save() wuerde in torch 2.10 torchcodec verlangen — das ist
                # bewusst NICHT installiert (CUDA-Build zerschiesst die GPU-Op-Registry).
                # Direkt via soundfile schreiben (wie der Rest des Servers). wav: (1, N).
                import soundfile as sf
                sf.write(str(out), wav.squeeze(0).detach().cpu().numpy(), model.sr)
                _touch()
                return f"Sprachausgabe erzeugt (clone, {lang}): {out}"
            except Exception as e:
                sys.stderr.write(f"[media-mcp] text_to_speech (clone) fehlgeschlagen: {e}\n")
                return f"(TTS-Klon fehlgeschlagen: {type(e).__name__}: {e})"
            finally:
                try:
                    os.unlink(ref_wav)
                except Exception:
                    pass

    # --- Piper-Pfad (CPU, Default) ---
    # KEIN GPU-_lock/VRAM noetig; aber espeak-ng (Piper-Phonemisierung) haelt
    # globalen C-State -> mit _tts_lock gegen sich selbst serialisieren (parallele
    # synthesize_wav-Aufrufe koennten sonst den Prozess segfaulten). KEIN _touch():
    # Piper nutzt keine GPU-Modelle -> darf den Idle-Reaper (Whisper/pyannote/ECAPA/
    # Chatterbox entladen) nicht hinauszoegern.
    import wave
    v = v[:2]
    if v not in PIPER_VOICES:
        v = "de"
    vp = PIPER_VOICES_DIR / PIPER_VOICES[v]
    if not vp.exists():
        return (f"(Stimme fehlt: {vp.name} — nachladen mit "
                f"'python -m piper.download_voices {vp.stem} --data-dir {PIPER_VOICES_DIR}')")
    try:
        from piper import PiperVoice
        with _tts_lock:
            if v not in _piper_cache:
                _piper_cache[v] = PiperVoice.load(str(vp))
            OUTPUTS.mkdir(parents=True, exist_ok=True)
            out = OUTPUTS / f"tts-{v}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.wav"
            with wave.open(str(out), "wb") as wf:
                _piper_cache[v].synthesize_wav(text, wf)
        return f"Sprachausgabe erzeugt ({v}): {out}"
    except Exception as e:
        sys.stderr.write(f"[media-mcp] text_to_speech fehlgeschlagen: {e}\n")
        return f"(TTS fehlgeschlagen: {type(e).__name__}: {e})"


def _publish_to_odysseus(out: pathlib.Path) -> str:
    """No-op: Odysseus wurde entfernt (2026-07-25) -> alles laeuft ueber Hermes.
    Hermes rendert generierte Bilder direkt aus dem base64-ImageContent, den
    _image_result zurueckgibt -- ein separater Publish-Ordner ist unnoetig.
    Bleibt als leere Funktion, damit _image_result unveraendert aufrufen kann
    (leerer Rueckgabewert -> die 'Direct link'-Zeile wird uebersprungen)."""
    return ""


def _image_result(out: pathlib.Path, prompt: str, size: str = "",
                  model: str = "SDXL 1.0 (ComfyUI)", note: str = ""):
    """Baut das MCP-Ergebnis [TextContent, ImageContent].

    Der Text folgt EXAKT dem Builtin-generate_image-Format
        Generated image for: <prompt>
        Direct link: /api/generated-image/<file>
        model: <model>
        size: <size>
    damit (a) das Modell den Link zuverlässig in seine Antwort echot -> inline-
    Bild/Link im Chat, und (b) Odysseus' _promote_image_fields-Regex greifen
    würde, falls der Tool-Name je 'generate_image' wäre. Zusätzlich wird der
    ImageContent zurückgegeben -> Odysseus hebt ihn als 'screenshot' in die
    Tool-Bubble (agent_loop result['images'] -> tool_output_data['screenshot'])."""
    fname = _publish_to_odysseus(out)
    lines = [f"Generated image for: {prompt[:200]}"]
    if fname:
        lines.append(f"Direct link: /api/generated-image/{fname}")
    lines.append(f"model: {model}")
    if size:
        lines.append(f"size: {size}")
    if note:
        lines.append(note)
    lines.append(f"(Datei: {out})")
    b64 = base64.b64encode(out.read_bytes()).decode()
    return [
        TextContent(type="text", text="\n".join(lines)),
        ImageContent(type="image", data=b64, mimeType="image/png"),
    ]


@mcp.tool()
def generate_image(prompt: str, negative_prompt: str = "", steps: int = 25,
                   width: int = 1024, height: int = 1024, seed: int = -1,
                   style: str = "realistic"):
    """Erzeugt ein Bild aus einem Text-Prompt über ComfyUI.

    Args:
        prompt: Bildbeschreibung (Englisch funktioniert am besten).
        negative_prompt: Was vermieden werden soll (optional).
        steps: Diffusions-Schritte (15-40, default 25).
        width, height: Auflösung — WÄHLE das Seitenverhältnis passend zum Motiv,
            NICHT stur 1024x1024. SDXL-Standardmaße (~1 MP, Vielfache von 64):
            • 1024x1024 quadratisch: Produkt, Symbol, Logo, Portrait-Nahaufnahme;
            • 832x1216 Hochformat: Person/Ganzkörper, Buchcover, Plakat, Menükarte,
              stehendes Schild;
            • 1216x832 Querformat: Landschaft, Panorama, Banner, breite Szene,
              Straßenschild, Gruppenbild.
            Bei Unsicherheit 1024x1024.
        seed: -1 = zufällig, sonst reproduzierbar.
        style: WICHTIG — passendes Modell zum Bildwunsch wählen:
            "realistic" = Fotos, Produkte, Menschen/Portraits, realistische Szenen (Standard);
            "anime"     = Anime, Manga, Cel-Shading;
            "artistic"  = Gemälde, Illustration, Comic, Fantasy, konzeptionelle Kunst;
            "flux"      = maximale Prompt-Treue UND lesbarer TEXT/Schrift im Bild
                          (Logos, Beschriftung, Poster, Schilder). Diesen Style nehmen,
                          wenn echte Buchstaben/Wörter im Bild stehen sollen ODER das
                          Motiv besonders komplex/präzise sein muss. Etwas langsamer;
            "qwen-image"= ABSOLUT bester Text (deutsche Umlaute ä/ö/ü, lange Wörter,
                          mehrsprachig). Nur nehmen, wenn PERFEKTER/kritischer Text
                          nötig ist oder flux beim Text nicht reicht. Deutlich langsamer.
            Bei Unsicherheit "realistic". Immer den Style setzen, der zum Motiv passt.
    """
    with _lock:
        _touch()
        style = _auto_style(prompt, style)
        steps = _clamp_steps(steps)
        width = _clamp_dim(width)
        height = _clamp_dim(height)
        seed_val = _pick_seed(seed)
        _ensure_vram(8_000_000_000)
        _comfy_ensure_running()
        _sty = _style(style)
        if _sty.get("qwen"):
            wf = _wf_qwen(prompt, negative_prompt, steps, width, height, seed_val, style)
        elif _sty.get("flux"):
            wf = _wf_flux(prompt, steps, width, height, seed_val, style)
        else:
            wf = _wf_txt2img(prompt, negative_prompt, steps, width, height, seed_val, style)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(out, prompt, size=f"{width}x{height}",
                             note=f"style: {_style(style).get('ckpt','?').split('.')[0]}, seed: {seed_val}")


@mcp.tool()
def edit_image(image: str, prompt: str, negative_prompt: str = "", strength: float = 0.55,
              steps: int = 25, seed: int = -1, style: str = "realistic"):
    """Bearbeitet ein bestehendes Bild per img2img (über ComfyUI).

    Args:
        image: Kennung des Quellbilds. Wurde das Bild in DIESEM Chat angehängt,
               den Wert aus dem 'Uploaded files attached to the latest user turn'-
               Manifest übergeben (bevorzugt id=, ersatzweise path=), NICHT den
               angezeigten Original-Dateinamen. Alternativ ein absoluter Pfad oder
               ein Dateiname in ~/ai/incoming/; die Suche erfolgt in ~/ai/incoming/
               und den Odysseus-Chat-Uploads (~/ai/odysseus/data/uploads/).
        prompt: Gewünschte Änderung/Zielbeschreibung.
        negative_prompt: Was vermieden werden soll (optional).
        strength: Denoise-Stärke 0.05-1.0 (default 0.55). Niedriger = näher am
                  Original, höher = mehr Freiheit/Veränderung.
        steps: Diffusions-Schritte (15-40, default 25).
        seed: -1 = zufällig, sonst reproduzierbar.
        style: Modell/Stil wie bei generate_image: "realistic" (Foto/Produkt/
               Portrait, Standard), "anime" (Anime/Manga), "artistic" (Gemälde/
               Comic/Fantasy/Illustration). Am besten zum Ziel-Look passend wählen.
    """
    with _lock:
        _touch()
        style = _auto_style(prompt, style)
        src = _resolve_media_file(image)
        steps = _clamp_steps(steps)
        strength = max(0.05, min(1.0, float(strength)))
        seed_val = _pick_seed(seed)
        _ensure_vram(8_000_000_000)
        _comfy_ensure_running()
        uploaded = _comfy_upload_image(src)
        tw, th = _sdxl_target_dims(src)
        wf = _wf_img2img(uploaded, prompt, negative_prompt, strength, steps, seed_val, tw, th, style)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(
            out, prompt, size=f"{tw}x{th}",
            note=f"edited: {pathlib.Path(src).name}, style: {_style(style).get('ckpt','?').split('.')[0]}, strength: {strength}, seed: {seed_val}")


@mcp.tool()
def inpaint_image(image: str, mask: str, prompt: str, negative_prompt: str = "",
                  steps: int = 25, seed: int = -1, style: str = "realistic"):
    """Füllt einen maskierten Bereich eines Bildes neu (Inpainting, SDXL über ComfyUI).

    Args:
        image: Kennung des Quellbilds. Wurde es in DIESEM Chat angehängt, den Wert
               aus dem 'Uploaded files attached to the latest user turn'-Manifest
               übergeben (bevorzugt id=, ersatzweise path=), NICHT den angezeigten
               Original-Dateinamen. Alternativ ein absoluter Pfad oder Dateiname in
               ~/ai/incoming/; die Suche erfolgt in ~/ai/incoming/ und den
               Odysseus-Chat-Uploads.
        mask: Kennung der Maske (weiß = zu bearbeitender Bereich, schwarz =
              unverändert) - gleiche Regeln/Suche wie bei image; ebenfalls die id=/
              path=-Kennung aus dem Upload-Manifest verwenden, falls angehängt.
        prompt: Was im maskierten Bereich entstehen soll.
        negative_prompt: Was vermieden werden soll (optional).
        steps: Diffusions-Schritte (15-40, default 25).
        seed: -1 = zufällig, sonst reproduzierbar.
        style: Modell/Stil wie bei generate_image: "realistic" (Standard),
               "anime", "artistic". Passend zum umgebenden Bild wählen.
    """
    with _lock:
        _touch()
        img_src = _resolve_media_file(image)
        mask_src = _resolve_media_file(mask)
        steps = _clamp_steps(steps)
        seed_val = _pick_seed(seed)
        _ensure_vram(8_000_000_000)
        _comfy_ensure_running()
        uploaded_img = _comfy_upload_image(img_src)
        uploaded_mask = _comfy_upload_image(mask_src)
        tw, th = _sdxl_target_dims(img_src)
        wf = _wf_inpaint(uploaded_img, uploaded_mask, prompt, negative_prompt, steps, seed_val, tw, th, style)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(
            out, prompt, size=f"{tw}x{th}",
            note=f"inpainted: {pathlib.Path(img_src).name}, seed: {seed_val}")


@mcp.tool()
def generate_video(prompt: str, negative_prompt: str = "worst quality, blurry, jittery, distorted",
                   width: int = 768, height: int = 512, length: int = 121, steps: int = 8,
                   fps: int = 25, seed: int = -1) -> str:
    """Erzeugt ein kurzes Video (mp4) aus einem Text-Prompt — lokal via LTX-Video 0.9.8
    (ComfyUI, GPU). Default 768x512, 121 Frames (~5s @25fps), 8 Steps (distilled).
    Laenger/groesser = langsamer. Braucht VRAM -> _ensure_vram entlaedt Ollama vorher.
    Args: prompt; negative_prompt; width/height (Vielfache von 32); length (Frames,
    ~4n+1); steps (distilled 8); fps; seed (-1=zufall). Returns: Pfad zur mp4."""
    with _lock:
        _touch()
        _ensure_vram(16_000_000_000)
        _comfy_ensure_running()
        wf = _wf_txt2video(prompt, negative_prompt, _clamp_dim(width), _clamp_dim(height),
                           int(length), int(steps), int(fps), _pick_seed(seed))
        out = _comfy_run_video(wf, timeout_s=600.0)
        _touch()
        return f"Video erzeugt: {out}"


@mcp.tool()
def generate_music(prompt: str, lyrics: str = "", seconds: float = 30.0,
                   steps: int = 8, seed: int = -1) -> str:
    """Erzeugt Musik/Audio aus einer Beschreibung — lokal via ACE-Step 1.5 (ComfyUI, GPU).
    `prompt` = Stil/Genre-Tags (z.B. 'upbeat lofi hip hop, mellow piano'); `lyrics`
    optional (Gesangstext, leer = instrumental); `seconds` Laenge (bis ~240). Braucht
    VRAM -> _ensure_vram entlaedt Ollama vorher. Returns: Pfad zur Audiodatei."""
    with _lock:
        _touch()
        _ensure_vram(12_000_000_000)
        _comfy_ensure_running()
        wf = _wf_music(prompt, lyrics, float(seconds), int(steps), _pick_seed(seed))
        out = _comfy_run_audio(wf, timeout_s=600.0)
        _touch()
        return f"Musik erzeugt: {out}"


if __name__ == "__main__":
    threading.Thread(target=_idle_reaper, daemon=True).start()
    sys.stderr.write(f"[media-mcp] SSE auf 0.0.0.0:{PORT}/sse  "
                     f"(idle-unload {IDLE_UNLOAD_S}s, ComfyUI @ {COMFY_URL})\n")
    mcp.run(transport="sse")
