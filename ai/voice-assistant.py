#!/usr/bin/env python3
"""Vollständig lokaler deutscher Sprachassistent.

Pipeline (alle Modelle einmalig beim Start geladen, dann resident):
  Mikrofon (parecord) -> Whisper-STT (cuda fp16) -> qwen3:30b (Ollama, Streaming)
  -> satzweise Piper-TTS (CPU) -> paplay.

Bedienung: Push-to-Talk. Enter startet die Aufnahme, ein zweites Enter beendet
sie (optionaler Energie-/Stille-Fallback als Zusatz). Während der Sprachausgabe
bricht ein Tastendruck die Wiedergabe ab (Barge-in).
"""

from __future__ import annotations

import os
import re
import sys
import queue
import select
import shutil
import signal
import tempfile
import termios
import threading
import wave

import numpy as np
import requests
import soundfile as sf

# --------------------------------------------------------------------------- #
# Konstanten (per Umgebungsvariable überschreibbar)
# --------------------------------------------------------------------------- #
MIC_DEVICE = os.environ.get("VA_MIC_DEVICE", "A50 Chat")
# Ausgabe-Senke getrennt von der Mikrofon-Quelle (F8): unabhängig routbar.
OUT_DEVICE = os.environ.get("VA_OUT_DEVICE", "A50 Chat")
PIPER_VOICE = os.environ.get(
    "VA_PIPER_VOICE",
    os.path.expanduser("~/ai/piper-voices/de_DE-thorsten-high.onnx"),
)
WHISPER_MODEL = os.environ.get("VA_WHISPER_MODEL", "openai/whisper-small")
OLLAMA_MODEL = os.environ.get("VA_OLLAMA_MODEL", "qwen3:30b")
OLLAMA_URL = os.environ.get("VA_OLLAMA_URL", "http://localhost:11434")

SAMPLE_RATE = 16000
STOP_SILENCE_SEC = 1.2      # Auto-Stopp nach so viel Stille (nach erster Sprache)
RMS_THRESHOLD = 300.0       # int16-RMS-Schwelle für "Sprache gehört"
CHUNK_MS = 100              # PCM-Leseblöcke in Millisekunden
SENTENCE_FLUSH_CHARS = 220  # Run-on-Guard: Puffer notfalls hart ausgeben
HISTORY_TURNS = 8           # Anzahl behaltener User/Assistant-Paare
OLLAMA_TIMEOUT = 120        # großzügig wegen qwen3-"Thinking"-Vorlauf

SYSTEM_PROMPT = (
    "Du bist ein knapper Sprachassistent. Antworte IMMER auf Deutsch, in 1-3 "
    "kurzen gesprochenen Saetzen. Keine Einleitung, keine Meta-Kommentare, "
    "keine Auflistungen, kein Markdown, kein Code. Antworte direkt mit der "
    "Antwort, so wie man es im Gespraech sagen wuerde."
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_SENTENCE_BOUNDARY = re.compile(r"[.!?]+[\"')\]]?\s+")


# --------------------------------------------------------------------------- #
# TTS-Player: eine Daemon-Thread-Queue, Piper einmal geladen, paplay seriell
# --------------------------------------------------------------------------- #
class TTSPlayer:
    """Nimmt fertige Sätze entgegen und spielt sie lückenlos nacheinander ab.

    Piper-Synthese von Satz N überlappt zeitlich mit der Generierung von
    Satz N+1, weil die Sätze sofort in die Queue geschoben werden. Die
    eigentliche Wiedergabe (paplay) läuft seriell im Worker-Thread.
    """

    def __init__(self, voice, device: str):
        self._voice = voice
        self._device = device
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._proc = None  # aktuell laufender paplay-Prozess
        self._lock = threading.Lock()
        self._generation = 0  # F2: bei Barge-in erhöht, entwertet in-flight-Sätze
        self._pending = 0     # F7: eigener Zähler statt Queue.unfinished_tasks
        self._wav_index = 0
        self._tmpdir = tempfile.mkdtemp(prefix="va-tts-")
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # -- öffentliche API ---------------------------------------------------- #
    def say(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            with self._lock:
                self._pending += 1
            self._queue.put(sentence)

    def idle(self) -> bool:
        """True, wenn nichts mehr in der Queue liegt und nichts spielt."""
        with self._lock:
            return self._pending == 0

    def barge_in_stop(self) -> None:
        """Bricht laufende Wiedergabe ab und leert die Queue."""
        # Generation erhöhen (F2): ein bereits entnommener Satz, der gerade
        # synthetisiert wird, erkennt sich beim Re-Check als veraltet.
        with self._lock:
            self._generation += 1
        # Queue leeren (eigenen pending-Zähler symmetrisch zurücksetzen).
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
                with self._lock:
                    self._pending -= 1
        # laufenden paplay-Prozess killen.
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                proc.kill()

    def shutdown(self) -> None:
        self.barge_in_stop()
        self._queue.put(None)
        # F7: Worker gebunden joinen und tmp-Verzeichnis (+ bis zu 8 wavs) räumen.
        self._thread.join(timeout=5.0)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- intern ------------------------------------------------------------- #
    def _next_wav_path(self) -> str:
        self._wav_index = (self._wav_index + 1) % 8
        return os.path.join(self._tmpdir, f"tts-{self._wav_index}.wav")

    def _worker(self) -> None:
        import subprocess

        while True:
            sentence = self._queue.get()
            if sentence is None:  # Shutdown-Sentinel
                self._queue.task_done()
                break
            try:
                # Generation beim Entnehmen festhalten (F2).
                with self._lock:
                    gen = self._generation
                wav_path = self._next_wav_path()
                with wave.open(wav_path, "wb") as wav_file:
                    self._voice.synthesize_wav(sentence, wav_file)
                proc = None
                # Nach der Synthese UND unmittelbar vor Popen erneut prüfen;
                # Popen + self._proc-Registrierung laufen unter demselben Lock,
                # damit ein gleichzeitiger barge_in_stop() den frisch
                # gestarteten paplay nicht verpasst.
                with self._lock:
                    if gen == self._generation:
                        proc = subprocess.Popen(
                            ["paplay", f"--device={self._device}", wav_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        self._proc = proc
                if proc is not None:
                    proc.wait()
            except Exception as exc:  # eine kaputte Ausgabe darf nicht crashen
                print(f"[TTS-Fehler] {exc}", file=sys.stderr)
            finally:
                with self._lock:
                    self._proc = None
                    self._pending -= 1
                self._queue.task_done()


# --------------------------------------------------------------------------- #
# Aufnahme (Push-to-Talk mit optionalem Stille-Fallback)
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self, device: str):
        self._device = device
        self._lock = threading.Lock()
        # Vom Hauptthread aufrufbare Terminate-Closure des aktiven record()-Calls.
        self._terminate_current = None

    def record(self, stop_event: threading.Event) -> bytes:
        """Läuft im eigenen Thread: liest rohes PCM bis stop_event gesetzt ist."""
        import subprocess

        frames = bytearray()
        chunk_bytes = int(SAMPLE_RATE * (CHUNK_MS / 1000.0)) * 2  # s16le = 2 Byte
        speech_heard = False
        silence_sec = 0.0

        # F1: proc ist eine Call-lokale Variable (keine geteilte Instanz-State),
        # damit ein veralteter Thread niemals einen neueren parecord killt.
        proc = subprocess.Popen(
            [
                "parecord",
                f"--device={self._device}",
                f"--rate={SAMPLE_RATE}",
                "--channels=1",
                "--format=s16le",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def _terminate() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()

        # Diese Closure für den Hauptthread veröffentlichen.
        with self._lock:
            self._terminate_current = _terminate

        try:
            while not stop_event.is_set():
                # F1: blockierendes read(); der Hauptthread killt parecord nach
                # stop_event.set() -> read() liefert EOF -> Schleife bricht sicher.
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                frames.extend(data)

                # Energie-/Stille-Fallback (optional, zusätzlich zu Enter).
                # F6: auf gerade Byte-Zahl kürzen (odd tail -> kein ValueError).
                even = data[: len(data) & ~1]
                samples = np.frombuffer(even, dtype=np.int16)
                if samples.size:
                    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                    if rms >= RMS_THRESHOLD:
                        speech_heard = True
                        silence_sec = 0.0
                    elif speech_heard:
                        silence_sec += CHUNK_MS / 1000.0
                        if silence_sec >= STOP_SILENCE_SEC:
                            stop_event.set()
                            break
        finally:
            _terminate()
            with self._lock:
                if self._terminate_current is _terminate:
                    self._terminate_current = None

        return bytes(frames)

    def terminate(self) -> None:
        """Vom Hauptthread aufrufbar: killt den aktuell laufenden parecord.

        Killen von parecord lässt ein blockierendes stdout.read() im
        Recorder-Thread mit EOF zurückkehren, sodass dieser deterministisch
        entsperrt (F1).
        """
        with self._lock:
            term = self._terminate_current
        if term is not None:
            term()


def record_utterance(recorder: Recorder) -> bytes:
    """Push-to-Talk: Enter startet, zweites Enter (oder Stille) stoppt."""
    # F3: veraltete Zeilen im Terminal-Puffer verwerfen, sonst startet ein
    # liegengebliebener Enter-Anschlag sofort eine Aufnahme (Stille).
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    input("\n[Enter drücken zum Sprechen] ")
    stop_event = threading.Event()
    result: dict[str, bytes] = {}

    def _run():
        result["pcm"] = recorder.record(stop_event)

    rec_thread = threading.Thread(target=_run, daemon=True)
    rec_thread.start()

    print("[... sprich, Enter zum Beenden ...]")
    # Auf Enter ODER auf den Stille-Fallback warten.
    while not stop_event.is_set():
        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
        if ready:
            sys.stdin.readline()
            break
    stop_event.set()
    # F1: parecord aus dem HAUPTthread killen BEVOR wir joinen; das lässt ein
    # blockierendes read() im Recorder-Thread mit EOF zurückkehren (sonst
    # könnte eine tote/schlafende Quelle den Thread ewig hängen lassen).
    recorder.terminate()
    rec_thread.join(timeout=3.0)
    return result.get("pcm", b"")


def pcm_to_wav(pcm: bytes, path: str) -> None:
    """Rohes 16k-mono-s16le-PCM als PCM_16-WAV schreiben."""
    pcm = pcm[: len(pcm) & ~1]  # F6: ungerades Tail-Byte kappen (kein ValueError)
    audio = np.frombuffer(pcm, dtype=np.int16)
    sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")


# --------------------------------------------------------------------------- #
# STT
# --------------------------------------------------------------------------- #
def transcribe(asr_pipeline, wav_path: str) -> str:
    out = asr_pipeline(
        wav_path,
        generate_kwargs={"language": "de"},
        chunk_length_s=30,
    )
    return (out.get("text") or "").strip()


# --------------------------------------------------------------------------- #
# LLM-Streaming + satzweise TTS
# --------------------------------------------------------------------------- #
def _visible_text(raw: str) -> str:
    """Entfernt <think>-Blöcke defensiv (Sicherheitsnetz gegen Leaks)."""
    text = _THINK_BLOCK.sub("", raw)
    idx = text.find("<think>")  # unabgeschlossener Block -> abschneiden
    if idx != -1:
        text = text[:idx]
    return text


def stream_chat(messages: list[dict], tts: TTSPlayer) -> str:
    """Streamt eine qwen3-Antwort und schiebt fertige Sätze sofort an die TTS."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
    }

    raw = ""       # kompletter akkumulierter message.content
    spoken_len = 0  # Länge des bereits an die TTS gegebenen sichtbaren Textes

    def flush(final: bool = False) -> None:
        nonlocal spoken_len
        visible = _visible_text(raw)
        remainder = visible[spoken_len:]
        # vollständige Sätze anhand von Satzgrenzen ausgeben.
        while True:
            match = _SENTENCE_BOUNDARY.search(remainder)
            if not match:
                break
            sentence = remainder[: match.end()].strip()
            if sentence:
                tts.say(sentence)
            spoken_len += match.end()
            remainder = remainder[match.end():]
        # Run-on-Guard: überlanger Puffer ohne Satzgrenze -> hart ausgeben.
        if not final and len(remainder) >= SENTENCE_FLUSH_CHARS:
            tts.say(remainder.strip())
            spoken_len += len(remainder)
        # am Ende Restpuffer ausgeben.
        if final and remainder.strip():
            tts.say(remainder.strip())
            spoken_len += len(remainder)

    interrupted = False

    with requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        stream=True,
        timeout=OLLAMA_TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        import json

        for line in resp.iter_lines():
            # F4: Barge-in bereits WÄHREND des Streamings ermöglichen. Ein
            # Tastendruck bricht ab; das Schließen des with-Blocks (break)
            # bricht die Generierung serverseitig ab.
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                sys.stdin.readline()  # Zeile konsumieren, damit sie nicht leakt (F3)
                tts.barge_in_stop()
                print("[Wiedergabe abgebrochen]")
                interrupted = True
                break
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            piece = (chunk.get("message") or {}).get("content", "")
            if piece:
                raw += piece
                flush(final=False)
            if chunk.get("done"):
                break

    # Nach Barge-in NICHT final flushen (würde abgebrochene Sätze neu einreihen).
    if not interrupted:
        flush(final=True)
    return _visible_text(raw).strip()


# --------------------------------------------------------------------------- #
# Modelle laden
# --------------------------------------------------------------------------- #
def load_models():
    print("Lade Modelle (einmalig) ...")
    import torch
    from transformers import pipeline
    from piper import PiperVoice

    print(f"  Whisper: {WHISPER_MODEL} (cuda, fp16) ...")
    asr = pipeline(
        "automatic-speech-recognition",
        model=WHISPER_MODEL,
        dtype=torch.float16,
        device="cuda",
    )

    print(f"  Piper:   {os.path.basename(PIPER_VOICE)} (CPU) ...")
    voice = PiperVoice.load(PIPER_VOICE)

    print("Modelle bereit.\n")
    return asr, voice


# --------------------------------------------------------------------------- #
# Hauptschleife
# --------------------------------------------------------------------------- #
def wait_for_playback(tts: TTSPlayer) -> None:
    """Wartet, bis die TTS fertig ist; ein Tastendruck bricht ab (Barge-in)."""
    # F3: liegengebliebene Zeilen (z.B. der zweite Enter zum Aufnahme-Stopp)
    # verwerfen, sonst löst er hier sofort einen Phantom-Barge-in aus.
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    while not tts.idle():
        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
        if ready:
            sys.stdin.readline()
            tts.barge_in_stop()
            print("[Wiedergabe abgebrochen]")
            return


def main() -> None:
    asr, voice = load_models()
    tts = TTSPlayer(voice, OUT_DEVICE)  # F8: Ausgabe-Senke unabhängig vom Mikro
    recorder = Recorder(MIC_DEVICE)

    # sauberer Ctrl-C-Ausstieg: laufende Subprozesse beenden.
    def _handle_sigint(_sig, _frame):
        print("\nBeende ...")
        try:
            recorder.terminate()
        except Exception:
            pass
        tts.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)

    history: list[dict] = []

    # kurze Begrüßung, direkt gesprochen.
    print("Sprachassistent bereit. Strg-C zum Beenden.")
    tts.say("Hallo, ich bin bereit.")
    wait_for_playback(tts)

    wav_path = os.path.join(tempfile.gettempdir(), "va-input.wav")

    while True:
        try:
            pcm = record_utterance(recorder)
            if len(pcm) < 2:
                print("[Nichts aufgenommen]")
                continue

            pcm_to_wav(pcm, wav_path)

            try:
                text = transcribe(asr, wav_path)
            except Exception as exc:
                print(f"[STT-Fehler] {exc}")
                continue

            if len(text) < 2:
                print("[Nichts verstanden]")
                continue

            print(f"Du: {text}")
            history.append({"role": "user", "content": text})

            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

            try:
                reply = stream_chat(messages, tts)
            except Exception as exc:
                print(f"[LLM-Fehler] {exc}")
                history.pop()  # fehlgeschlagenen Turn nicht behalten
                continue

            print(f"Assistent: {reply}")
            history.append({"role": "assistant", "content": reply})

            # Historie auf die letzten HISTORY_TURNS Paare trimmen.
            if len(history) > HISTORY_TURNS * 2:
                history[:] = history[-HISTORY_TURNS * 2:]

            wait_for_playback(tts)

        except KeyboardInterrupt:
            _handle_sigint(None, None)
        except EOFError:
            # F5: EOF auf stdin (z.B. Pipe geschlossen) -> sauber beenden statt
            # in der catch-all-Klausel in eine 100%-CPU-Endlosschleife zu laufen.
            print("\n[EOF] Beende ...")
            tts.shutdown()
            break
        except Exception as exc:  # Schleife pro Turn robust halten
            print(f"[Fehler] {exc}")
            continue


if __name__ == "__main__":
    main()
