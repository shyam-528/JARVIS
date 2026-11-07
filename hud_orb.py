"""JARVIS Orb — Siri-style floating voice overlay.

A small glowing orb on the desktop. Click it (or Ctrl+Alt+J) and talk:
ripple animation while you speak, spin while the brain thinks, audio-reactive
pulse while Sonia answers. Voice in, voice out — no text here; the full
transcript lives on the web dashboard.

Modes: idle → listening → thinking → speaking → idle.
Brain: POST /api/chat (HUD mints its own token via AuthManager).
Audio: /api/voice/say (server-side distill + edge chain) played locally.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
API = "http://127.0.0.1:8080"
CHROMA = "#010101"  # near-black; keyed transparent on Windows

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_ERROR = "error"

_STATE_COLORS = {
    STATE_IDLE: [(99, 102, 241), (165, 180, 252), (49, 46, 129)],
    STATE_LISTENING: [(56, 189, 248), (165, 243, 252), (12, 74, 110)],
    STATE_THINKING: [(167, 139, 250), (196, 181, 253), (76, 29, 149)],
    STATE_SPEAKING: [(52, 211, 153), (165, 243, 208), (6, 78, 59)],
    STATE_ERROR: [(248, 113, 113), (254, 202, 202), (127, 29, 29)],
}


class OrbStateMachine:
    """Pure transition logic — unit-tested without a GUI."""

    ALLOWED = {
        STATE_IDLE: {STATE_LISTENING},
        STATE_LISTENING: {STATE_THINKING, STATE_IDLE, STATE_ERROR},
        STATE_THINKING: {STATE_SPEAKING, STATE_IDLE, STATE_ERROR},
        STATE_SPEAKING: {STATE_IDLE},
        STATE_ERROR: {STATE_IDLE},
    }

    def __init__(self):
        self.state = STATE_IDLE

    def go(self, target: str) -> bool:
        if target in self.ALLOWED.get(self.state, set()):
            self.state = target
            return True
        return False


def render_orb(size: int, colors: list[tuple[int, int, int]]):
    """Conic-gradient orb (the JARVIS mark): PIL image with dark core."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size / 2
    r = size / 2 - 2
    steps = len(colors)
    for deg in range(360):
        t = (deg / 360.0) * steps
        i = int(t) % steps
        f = t - int(t)
        a, b = colors[i], colors[(i + 1) % steps]
        col = tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))
        d.pieslice([c - r, c - r, c + r, c + r], start=deg, end=deg + 2, fill=col + (255,))
    # dark core + bright center dot (the JARVIS eye)
    ri = r * 0.52
    d.ellipse([c - ri, c - ri, c + ri, c + ri], fill=(10, 10, 12, 255))
    rd = r * 0.2
    d.ellipse([c - rd, c - rd, c + rd, c + rd], fill=colors[1] + (255,))
    return img


def rms_envelope(
    pcm_bytes: bytes, sample_rate: int, channels: int, frame_ms: int = 33
) -> list[float]:
    """Per-frame RMS (0..1) of decoded audio — drives the speaking pulse."""
    import numpy as np

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    per = max(1, int(sample_rate * frame_ms / 1000))
    out = []
    for i in range(0, len(samples), per):
        chunk = samples[i : i + per]
        if chunk.size:
            out.append(float(np.sqrt(np.mean(chunk**2))))
    peak = max(out) if out else 1.0
    return [min(1.0, v / (peak or 1.0)) for v in out]


def mint_or_load_token() -> str:
    """HUD's own API token. Minted directly via AuthManager (local file
    access — bypasses the HTTP bootstrap rule by design, loopback only)."""
    import secrets

    tok_file = DATA / "hud_token.txt"
    try:
        if tok_file.exists():
            tok = tok_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:
        pass
    from api.auth import AuthManager

    mgr = AuthManager(DATA / "api_tokens.db")
    token = mgr.create_token("hud-orb", 3650)
    if isinstance(token, dict):
        token = token.get("token", "")
    with contextlib.suppress(Exception):
        tok_file.write_text(token or secrets.token_hex(24), encoding="utf-8")
    return token


def api_post(path: str, payload: dict, token: str, timeout: float = 120.0, raw: bool = False):
    """Small sync POST helper."""
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if raw:
            return body, resp.headers.get("Content-Type", "")
        return json.loads(body.decode("utf-8"))


class OrbHUD:
    """The floating orb window + voice loop."""

    WIN = 260
    IDLE_SIZE = 60
    ACTIVE_SIZE = 110

    def __init__(self):
        import tkinter as tk

        self.tk = tk
        self.sm = OrbStateMachine()
        self.level = 0.0  # 0..1 live audio level (mic or playback)
        self._level_lock = threading.Lock()
        self._spin = 0  # rotation frame index
        self._phase = 0.0  # breathing clock
        self._active_until = 0.0  # expanded-window timer
        self._worker: threading.Thread | None = None
        self._token = ""
        self._transcriber = None
        self._frames: dict = {}  # state -> [PhotoImage rotations]

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA)
        self.root.configure(bg=CHROMA)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.WIN}x{self.WIN}+{sw - self.WIN - 40}+{sh - self.WIN - 120}")

        self.canvas = tk.Canvas(
            self.root,
            width=self.WIN,
            height=self.WIN,
            bg=CHROMA,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        self._img_ref = None

        # drag + click-to-talk
        self._drag = (0, 0)
        self._down_at = 0.0
        self.canvas.bind("<Button-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_up)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        try:
            import keyboard

            keyboard.add_hotkey("ctrl+alt+j", self.toggle_listen)
        except Exception:
            pass

        self._prerender()

    # ---------- rendering ----------

    def _prerender(self):
        """Pre-render 24 rotation frames per state at master size."""
        master = 220
        for state, colors in _STATE_COLORS.items():
            base = render_orb(master, colors)
            self._frames[state] = [base.rotate(a, resample=2) for a in range(0, 360, 15)]

    def _display_size(self) -> int:
        state = self.sm.state
        if state == STATE_IDLE:
            breath = 1.0 + 0.06 * math.sin(self._phase)
            return int(self.IDLE_SIZE * breath)
        with self._level_lock:
            lv = self.level
        if state in (STATE_LISTENING, STATE_SPEAKING):
            return int(self.ACTIVE_SIZE * (0.85 + 0.45 * lv))
        return self.ACTIVE_SIZE

    def _tick(self):
        from PIL import ImageTk

        self._phase += 0.06
        state = self.sm.state
        speed = {
            STATE_IDLE: 0,
            STATE_LISTENING: 1,
            STATE_THINKING: 3,
            STATE_SPEAKING: 1,
            STATE_ERROR: 0,
        }.get(state, 1)
        self._spin = (self._spin + speed) % 24
        frame = self._frames[state][self._spin]
        size = self._display_size()
        img = frame.resize((size, size), resample=2)
        photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(self.WIN // 2, self.WIN // 2, image=photo)
        self._img_ref = photo  # keep a reference
        # idle shrink-back: collapse to the small orb after inactivity
        if state == STATE_IDLE and self._active_until and time.time() > self._active_until:
            self._active_until = 0
        self.root.after(33, self._tick)

    # ---------- input ----------

    def _on_down(self, e):
        self._drag = (e.x, e.y)
        self._down_at = time.time()

    def _on_drag(self, e):
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _on_up(self, e):
        if time.time() - self._down_at < 0.3:
            self.toggle_listen()

    # ---------- voice loop ----------

    def toggle_listen(self):
        if self.sm.state != STATE_IDLE:
            return
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._voice_roundtrip, daemon=True)
        self._worker.start()

    def _set_level(self, v: float):
        with self._level_lock:
            self.level = max(0.0, min(1.0, v))

    def _voice_roundtrip(self):
        if not self.sm.go(STATE_LISTENING):
            return
        try:
            text = self._listen()
            if not text:
                self.sm.go(STATE_IDLE)
                return
            self.sm.go(STATE_THINKING)
            reply = self._ask_brain(text)
            if not reply:
                self.sm.go(STATE_ERROR)
                time.sleep(1.2)
                self.sm.go(STATE_IDLE)
                return
            self.sm.go(STATE_SPEAKING)
            self._speak(reply)
            self.sm.go(STATE_IDLE)
            self._active_until = time.time() + 5
        except Exception:
            self.sm.go(STATE_ERROR)
            time.sleep(1.2)
            self.sm.go(STATE_IDLE)

    def _mic_locked(self) -> bool:
        lock = DATA / "mic.lock"
        try:
            return lock.exists() and (time.time() - lock.stat().st_mtime) < 30
        except Exception:
            return False

    def _listen(self) -> str:
        if self._mic_locked():
            return ""
        from voice.mic import record_vad

        lock = DATA / "mic.lock"
        with contextlib.suppress(Exception):
            lock.write_text(str(time.time()), encoding="utf-8")
        try:
            pcm = record_vad(
                silence_ms=900,
                max_s=15.0,
                on_level=lambda lvl: self._set_level(1.0 if lvl == "speech" else 0.0),
            )
        finally:
            with contextlib.suppress(Exception):
                lock.unlink(missing_ok=True)
        if pcm.size == 0:
            return ""
        if self._transcriber is None:
            from voice.stt import Transcriber

            self._transcriber = Transcriber(model_size="small")
        return self._transcriber.transcribe(pcm)

    def _ask_brain(self, text: str) -> str:
        if not self._token:
            self._token = mint_or_load_token()
        try:
            res = api_post("/api/chat", {"message": text}, self._token)
            return (res.get("response") or "").strip()
        except Exception:
            return ""

    def _speak(self, reply: str):
        """Server-side distill+TTS, local audio-reactive playback."""
        try:
            body, ctype = api_post("/api/voice/say", {"text": reply[:1200]}, self._token, raw=True)
        except Exception:
            return
        if not body or len(body) < 500:
            return
        import miniaudio
        import numpy as np
        import sounddevice as sd

        try:
            dec = miniaudio.decode(body)
        except Exception:
            return
        raw = np.frombuffer(bytes(dec.samples), dtype=np.int16).astype(np.float32) / 32768.0
        if dec.nchannels == 2:
            raw = raw.reshape(-1, 2)
        env = rms_envelope(bytes(dec.samples), dec.sample_rate, dec.nchannels)
        sd.play(raw, samplerate=dec.sample_rate, blocking=False)
        started = time.monotonic()
        frame_s = 0.033
        while sd.get_stream().active:
            idx = int((time.monotonic() - started) / frame_s)
            self._set_level(env[idx] if 0 <= idx < len(env) else 0.2)
            time.sleep(frame_s)
        sd.stop()
        self._set_level(0.0)

    def run(self):
        self.root.after(33, self._tick)
        self.root.mainloop()
