"""Wake-word listener ("hey jarvis") on openwakeword.

Contract: listen(should_stop) blocks on the mic until the stop
predicate is true -- run it in a thread (mic.record_vad spins it as a
daemon). The model downloads/loads lazily on first listen; load and
stream failures raise to the caller: a dead wake listener must not be
mistaken for a quiet room. Detector scores reset after each trigger so
one utterance cannot double-fire.
"""

from __future__ import annotations

from collections.abc import Callable


class WakeListener:
    FRAME_MS = 80

    def __init__(self, threshold: float = 0.5, samplerate: int = 16000):
        self.threshold = threshold
        self.samplerate = samplerate
        self.frame_len = int(samplerate * self.FRAME_MS / 1000)
        self._model = None

    def _load(self):
        if self._model is None:
            from openwakeword.model import Model
            from openwakeword.utils import download_models

            download_models(model_names=["hey_jarvis"])
            self._model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        return self._model

    def listen(
        self,
        on_wake: Callable[[], None],
        should_stop: Callable[[], bool] | None = None,
        cooldown_s: float = 3.0,
    ) -> None:
        import sounddevice as sd

        model = self._load()
        model.reset()
        cooldown_frames = int(cooldown_s * 1000 / self.FRAME_MS)
        cooldown_left = 0

        with sd.InputStream(
            samplerate=self.samplerate, channels=1, dtype="int16", blocksize=self.frame_len
        ) as stream:
            while not (should_stop and should_stop()):
                data, _overflow = stream.read(self.frame_len)
                chunk = data[:, 0]
                scores = model.predict(chunk)
                score = max(scores.values()) if scores else 0.0
                if cooldown_left > 0:
                    cooldown_left -= 1
                    continue
                if score >= self.threshold:
                    model.reset()
                    cooldown_left = cooldown_frames
                    on_wake()
