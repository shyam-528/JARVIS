"""Speech-to-text via faster-whisper (model lazy-loaded on first use).

Contract: transcribe(pcm16, sample_rate) takes int16 mono PCM and
resamples to 16 kHz when needed; transcribe_file(path) handles container
formats via the model's own decoder. Both return plain text ("" for
empty input). Model load and inference failures raise to the caller:
STT has no silent mode -- an unusable transcriber must be visible, not
mistaken for silence.
"""

from __future__ import annotations

import numpy as np


class Transcriber:
    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 4,
    ):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
            )
        return self._model

    def transcribe(
        self, pcm16: np.ndarray, samplerate: int = 16000, language: str | None = None
    ) -> str:
        if pcm16.size == 0:
            return ""
        if samplerate != 16000:
            ratio = samplerate / 16000
            indices = (np.arange(0, int(pcm16.size / ratio)) * ratio).astype(int)
            pcm16 = pcm16[indices]
        audio = pcm16.astype("float32") / 32768.0
        model = self._load()
        segments, _info = model.transcribe(audio, language=language, beam_size=1, vad_filter=False)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def transcribe_file(self, path: str, language: str | None = None) -> str:
        model = self._load()
        segments, _info = model.transcribe(path, language=language, beam_size=1)
        return " ".join(seg.text.strip() for seg in segments).strip()
