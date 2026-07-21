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
import os, sys, time, uuid, threading, subprocess, datetime, pathlib, json, base64
import urllib.request, urllib.error, urllib.parse

HOME = pathlib.Path.home()
AI = HOME / "ai"
INCOMING = AI / "incoming"          # hier abgelegte Dateien per Kurzname erreichbar
UPLOADS = AI / "odysseus" / "data" / "uploads"   # Odysseus-Chat-Uploads (verschachtelt nach Datum)
OUTPUTS = AI / "media-mcp" / "outputs"
# Odysseus serviert Bilder unter /api/generated-image/<file>; der Dateiname MUSS
# ^[a-f0-9]{8,64}\.(png|...) matchen (src/generated_images.py). Wir legen jedes
# erzeugte/bearbeitete Bild zusätzlich hier ab, damit es inline im Chat rendert.
GENERATED_IMAGES = AI / "odysseus" / "data" / "generated_images"
IDLE_UNLOAD_S = int(os.environ.get("MEDIA_MCP_IDLE_UNLOAD_S", "180"))
PORT = int(os.environ.get("MEDIA_MCP_PORT", "8765"))
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")

COMFY_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
COMFY_LAUNCH_SCRIPT = str(AI / "comfyui.sh")
COMFY_CKPT = "sd_xl_base_1.0.safetensors"
COMFY_START_TIMEOUT_S = 120

INCOMING.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

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
                if _state["whisper"]:
                    _state["whisper"].clear()
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    _state["last_use"] = 0.0
                    sys.stderr.write("[media-mcp] idle -> Whisper entladen, VRAM frei\n")


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
    roots = (INCOMING, UPLOADS, OUTPUTS, GENERATED_IMAGES)
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
                 seed: int) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFY_CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt or "",
                                                          "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height),
                                                            "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed_val, "steps": int(steps), "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_img2img(uploaded: dict, prompt: str, negative_prompt: str, strength: float,
                 steps: int, seed: int, tw: int, th: int) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    img_ref = uploaded["name"]
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFY_CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt or "",
                                                          "clip": ["1", 1]}},
        "8": {"class_type": "LoadImage", "inputs": {"image": img_ref}},
        # Defect-1-Fix: auf SDXL-Auflösung (~1024 lange Seite) hochskalieren, BEVOR
        # VAEEncode -> sonst 'color bomb' bei kleinen Eingaben.
        "11": {"class_type": "ImageScale", "inputs": {
            "image": ["8", 0], "upscale_method": "lanczos",
            "width": int(tw), "height": int(th), "crop": "disabled"}},
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["1", 2]}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["9", 0],
            "seed": seed_val, "steps": int(steps), "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": float(strength)}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
    }


def _wf_inpaint(uploaded_image: dict, uploaded_mask: dict, prompt: str, negative_prompt: str,
                 steps: int, seed: int, tw: int, th: int) -> dict:
    seed_val = int(seed) if seed is not None and seed >= 0 else uuid.uuid4().int & 0xFFFFFFFF
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFY_CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt or "",
                                                          "clip": ["1", 1]}},
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
            "seed": seed_val, "steps": int(steps), "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "media-mcp"}},
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


def _comfy_run(workflow: dict, prompt: str, timeout_s: float = 300.0) -> pathlib.Path:
    """Submitted einen Workflow, wartet auf das Ergebnis, speichert es unter
    ~/ai/media-mcp/outputs/img-<ts>.png und gibt den Pfad zurück."""
    prompt_id = _comfy_submit(workflow)
    try:
        images = _comfy_wait_result(prompt_id, timeout_s=timeout_s)
        data = _comfy_fetch_image(images[0])
    finally:
        _comfy_free_vram()          # VRAM zurück an Ollama, auch bei Fehler
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = OUTPUTS / f"img-{ts}.png"
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


@mcp.tool()
def transcribe(file: str, language: str = "auto") -> str:
    """Transkribiert eine Audio- oder Videodatei (auch .mp4/.mkv/.mov) mit Whisper auf der GPU.

    Args:
        file: Absoluter Pfad zur Datei ODER Dateiname, wenn sie in ~/ai/incoming/ liegt.
        language: 'auto' (default, DE/EN-Erkennung), 'de', 'en', oder
                  'swiss'/'gsw' für Schweizerdeutsch (Output = Hochdeutsch).
    Returns:
        Den transkribierten Text.
    """
    with _lock:
        _touch()
        # _resolve_media_file (nicht _resolve_file): sucht auch in den Odysseus-
        # Chat-Uploads (~/ai/odysseus/data/uploads/), damit im Chat angehängte
        # Audio-/Videodateien (mp4 etc.) transkribiert werden können. Bleibt auf
        # incoming/uploads/outputs eingesperrt (Confinement in _resolve_media_file).
        src = _resolve_media_file(file)
        wav = _to_wav(src)
        try:
            lang = language.lower().strip()
            if lang in ("swiss", "gsw", "ch", "schweizerdeutsch"):
                model_id = "Flurin17/whisper-large-v3-turbo-swiss-german"
                gen = {"language": "de"}
            else:
                model_id = "openai/whisper-large-v3"
                gen = {} if lang in ("", "auto") else {"language": lang}
            _ensure_vram(5_000_000_000)      # ~5 GB für Whisper large-v3
            asr = _whisper(model_id)
            # batch_size=2 statt 8: batch=8 ließ Whisper bei langen Videos ~10GB VRAM
            # belegen -> Kollision mit residentem LLM -> HIP-Hardware-Exception (0x1016).
            # batch=2 hält Whisper bei ~4-5GB, koexistiert stabil. Etwas langsamer.
            res = asr(wav, chunk_length_s=30, batch_size=2, generate_kwargs=gen,
                      return_timestamps=False)
            _touch()
            text = res["text"].strip()
            return text or "(leeres Transkript)"
        finally:
            try:
                os.unlink(wav)
            except Exception:
                pass


def _publish_to_odysseus(out: pathlib.Path) -> str:
    """Kopiert das erzeugte PNG in Odysseus' generated_images-Verzeichnis unter
    einem Hex-Dateinamen (matcht ^[a-f0-9]{8,64}\\.png$, das einzige Format, das
    /api/generated-image/<file> serviert). Rückgabe: Dateiname oder '' bei Fehler.
    So kann das Bild inline im Chat gerendert werden, sobald das Modell den
    'Direct link' in seiner Antwort echot (bzw. über die Galerie)."""
    try:
        GENERATED_IMAGES.mkdir(parents=True, exist_ok=True)
        fname = uuid.uuid4().hex[:16] + ".png"
        (GENERATED_IMAGES / fname).write_bytes(out.read_bytes())
        return fname
    except Exception as e:
        sys.stderr.write(f"[media-mcp] generated_images-Publish fehlgeschlagen: {e}\n")
        sys.stderr.flush()
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
                   width: int = 1024, height: int = 1024, seed: int = -1):
    """Erzeugt ein Bild aus einem Text-Prompt mit Stable Diffusion XL über ComfyUI.

    Args:
        prompt: Bildbeschreibung (Englisch funktioniert am besten).
        negative_prompt: Was vermieden werden soll (optional).
        steps: Diffusions-Schritte (15-40, default 25).
        width, height: Auflösung (default 1024x1024, SDXL-optimiert).
        seed: -1 = zufällig, sonst reproduzierbar.
    """
    with _lock:
        _touch()
        steps = _clamp_steps(steps)
        width = _clamp_dim(width)
        height = _clamp_dim(height)
        seed_val = _pick_seed(seed)
        _ensure_vram(8_000_000_000)
        _comfy_ensure_running()
        wf = _wf_txt2img(prompt, negative_prompt, steps, width, height, seed_val)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(out, prompt, size=f"{width}x{height}",
                             note=f"seed: {seed_val}")


@mcp.tool()
def edit_image(image: str, prompt: str, negative_prompt: str = "", strength: float = 0.55,
              steps: int = 25, seed: int = -1):
    """Bearbeitet ein bestehendes Bild per img2img (SDXL über ComfyUI).

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
    """
    with _lock:
        _touch()
        src = _resolve_media_file(image)
        steps = _clamp_steps(steps)
        strength = max(0.05, min(1.0, float(strength)))
        seed_val = _pick_seed(seed)
        _ensure_vram(8_000_000_000)
        _comfy_ensure_running()
        uploaded = _comfy_upload_image(src)
        tw, th = _sdxl_target_dims(src)
        wf = _wf_img2img(uploaded, prompt, negative_prompt, strength, steps, seed_val, tw, th)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(
            out, prompt, size=f"{tw}x{th}",
            note=f"edited: {pathlib.Path(src).name}, strength: {strength}, seed: {seed_val}")


@mcp.tool()
def inpaint_image(image: str, mask: str, prompt: str, negative_prompt: str = "",
                  steps: int = 25, seed: int = -1):
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
        wf = _wf_inpaint(uploaded_img, uploaded_mask, prompt, negative_prompt, steps, seed_val, tw, th)
        out = _comfy_run(wf, prompt, timeout_s=_comfy_timeout_for(steps))
        _touch()
        return _image_result(
            out, prompt, size=f"{tw}x{th}",
            note=f"inpainted: {pathlib.Path(img_src).name}, seed: {seed_val}")


if __name__ == "__main__":
    threading.Thread(target=_idle_reaper, daemon=True).start()
    sys.stderr.write(f"[media-mcp] SSE auf 0.0.0.0:{PORT}/sse  "
                     f"(idle-unload {IDLE_UNLOAD_S}s, ComfyUI @ {COMFY_URL})\n")
    mcp.run(transport="sse")
