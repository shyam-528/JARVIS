"""Speech distiller: condense long LLM replies into short spoken lines.

Contract: spoken_version(full_text, user_text) is a pure async transform ->
the text JARVIS should say out loud. Short replies pass through verbatim;
long ones go through the LLM with a per-question prompt (adaptive budgets).

Adaptive sizing (mirrors core/verbosity.py for answers): the question sets
the speech budget — simple questions get one short sentence and NO pointer
(the line stands alone); complex ones get up to three sentences plus a
fresh, question-fitted pointer clause the MODEL writes (never a fixed pool
pick, never repeating the previous turn's phrasing).

Taste memory (voice/prefs.py): corrections like "don't call me sir" or
"too long" persist in data/voice_prefs.json and shape every future line.

Failure policy: after 3 consecutive LLM failures the distiller latches
off for the session (_broken) and spoken_version falls back to sentence
truncation (+ the legacy fixed pointer only in that outage path, so speech
still points at the screen). Speech never breaks, and the latch stops
burning a timeout on every turn while the provider is down.
"""

from __future__ import annotations

import random
import re

from core.config import ROOT, Config
from core.router import FailoverRouter
from core.verbosity import score_question
from voice.prefs import VoicePrefs
from voice.tts import sanitize_for_speech, split_sentences

# Distill only when meaningfully long — short replies speak verbatim.
DISTILL_THRESHOLD = 200
MAX_SPOKEN_CHARS = 300

# Speech budgets per question level: (max sentences, max words, max chars).
# Simple questions get a headline with no pointer; complex ones earn detail
# plus a generated pointer clause.
SPEECH_BUDGETS = {
    "short": (1, 25, 160),
    "normal": (2, 45, 300),
    "detailed": (3, 70, 450),
}

_PERSONAS = {
    "butler": "British butler tone: calm, precise, dry wit.",
    "plain": "Neutral, warm, direct tone. No persona, no theatrics, no titles.",
}


def _build_prompt(
    level: str,
    *,
    address: str,
    persona: str,
    has_screen: bool,
    last_spoken: str,
) -> str:
    """Assemble the per-question distill prompt (no fixed recipe)."""
    sentences, words, _ = SPEECH_BUDGETS.get(level, SPEECH_BUDGETS["normal"])
    persona_line = _PERSONAS.get(persona, _PERSONAS["butler"])
    if address == "none":
        address_line = 'Never say "sir" or any other title.'
    elif address and address != "sir":
        address_line = f'Address the user as "{address}" at most once. Never say "sir".'
    else:
        address_line = 'Address the user as "sir" at most once.'
    if has_screen:
        avoid = (last_spoken or "")[:160].replace('"', "'")
        pointer_rule = (
            "The full reply is on the screen. If your spoken line leaves out "
            "anything important, end with ONE fresh short pointer clause fitted "
            "to this specific reply (a few words naming what is on screen). "
            f'Never reuse this phrasing: "{avoid}". '
            "If the spoken line already stands alone, end cleanly with no pointer."
        )
    else:
        pointer_rule = (
            "There is no screen. Never mention one, never point at one, "
            'never say anything like "on screen".'
        )
    return f"""You are the speech-writer for JARVIS, an AI assistant. The user just
asked something and JARVIS produced a full written reply. Your job: produce
what JARVIS SAYS ALOUD.

Rules:
- At most {sentences} sentence(s), under {words} words total. This is speech,
  not a summary paragraph.
- Lead with the direct answer or outcome. Add detail only if it changes
  what the user does next.
- {persona_line} {address_line}
- No markdown, no lists, no code, no URLs, no emoji, no stage directions.
- Never drop numbers, names, or times — round statistics but keep every
  figure that changes what the user does. If the reply is a list, speak the
  count plus the headline item; never mash a list into one sentence.
- Never invent facts. If the reply is mostly code or data tables, name what
  it contains in one short clause.
- {pointer_rule}
- Output ONLY the words to speak. No quotes, no labels."""


# Back-compat: the old fixed recipe (kept for reference; the live path now
# builds its prompt per question via _build_prompt).
DISTILL_PROMPT = _build_prompt(
    "normal", address="sir", persona="butler", has_screen=True, last_spoken=""
)


class SpeechDistiller:
    """Condenses full LLM replies into short spoken lines, sized by question.

    The complete text still goes to the screen/chat; only the spoken part is
    distilled. Falls back to sentence-truncation when the LLM is unavailable,
    so speech never breaks.
    """

    def __init__(self, router: FailoverRouter, config: Config, prefs_path=None):
        self.router = router
        self.config = config
        self.timeout_s = 6.0
        self._broken = False  # set after repeated failures; stop trying for this session
        self._fail_count = 0
        # Configurable via voice.speech.distill / distill_threshold
        self.enabled = bool(getattr(config.voice, "distill_speech", True))
        self.threshold = int(getattr(config.voice, "distill_threshold", DISTILL_THRESHOLD))
        self.persona = str(getattr(config.voice, "distill_persona", "butler") or "butler")
        address_seed = str(getattr(config.voice, "distill_address", "sir") or "sir")
        try:
            default_path = ROOT / "data" / "voice_prefs.json"
        except Exception:
            default_path = "data/voice_prefs.json"
        self.prefs = VoicePrefs.load(prefs_path or default_path, address_seed=address_seed)

    # ---------- public ----------

    def speech_level(self, user_text: str = "") -> str:
        """Size this question's speech: short | normal | detailed.

        The question's own level wins (mirrors answer sizing); a standing
        brevity taste only nudges: "brief" caps at headline, "full" floors
        at the longer line. Never raises.
        """
        try:
            taste = (self.prefs.brevity or "auto").lower()
        except Exception:
            taste = "auto"
        return level_for(user_text, taste)

    async def spoken_version(
        self, full_reply: str, user_text: str = "", has_screen: bool = True
    ) -> str:
        """Return the short text JARVIS should say aloud for this reply."""
        clean = " ".join(full_reply.split())
        limit = self.threshold if self.enabled else 10**9
        if len(clean) <= limit:
            return clean  # short enough — speak verbatim

        level = self.speech_level(user_text)
        max_chars = SPEECH_BUDGETS[level][2]

        distilled = (
            await self._distill(clean, user_text, level, has_screen) if self.enabled else None
        )
        if distilled:
            if len(distilled) > max_chars:
                distilled = _truncate_sentences(distilled, max_chars)[0]
            self._remember(distilled)
            return distilled

        # Fallback: first sentence(s) up to budget (old behavior, but tighter)
        spoken, remainder = _truncate_sentences(clean, max_chars)
        if remainder:
            spoken = spoken.rstrip(".!,? ") + "."
            if has_screen:
                # Outage path only: the model is down so it cannot write a
                # fresh pointer — the legacy fixed pool keeps speech pointing
                # at the screen instead of going silent about it.
                spoken += detail_offer(spoken)
        self._remember(spoken)
        return spoken

    # ---------- internals ----------

    def _remember(self, spoken: str) -> None:
        """Store the last spoken line for the no-repeat guard. Best-effort."""
        try:
            self.prefs.last_spoken = spoken
            self.prefs.save()
        except Exception:
            pass

    async def _distill(
        self, full_reply: str, user_text: str, level: str, has_screen: bool
    ) -> str | None:
        if self._broken:
            return None
        try:
            address = self.prefs.address
        except Exception:
            address = "sir"
        try:
            last = self.prefs.last_spoken or ""
        except Exception:
            last = ""
        user_part = f"User asked: {user_text[:200]}" if user_text else ""
        prompt = f"{user_part}\n\nFull reply:\n{full_reply[:2500]}"
        system = _build_prompt(
            level, address=address, persona=self.persona, has_screen=has_screen, last_spoken=last
        )
        try:
            out: list[str] = []
            async for _, chunk in self.router.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                task_type="fast",
            ):
                if isinstance(chunk, str):
                    out.append(chunk)
            text = "".join(out).strip()
            if not text:
                return None
            # Clean model artifacts: quotes, labels, markdown leftovers
            text = text.strip('"`*_#')
            text = sanitize_for_speech(text)
            if text:
                self._fail_count = 0
                return text
            return None
        except Exception:
            self._fail_count += 1
            if self._fail_count >= 3:
                self._broken = True  # stop burning time; use fallback silently
            return None


def _truncate_sentences(text: str, max_chars: int) -> tuple[str, str]:
    """Split into (head, remainder) at a sentence boundary near max_chars."""
    sentences = split_sentences(text)
    head: list[str] = []
    length = 0
    taken = 0
    for part in sentences:
        if head and length + len(part) + 1 > max_chars:
            break
        head.append(part)
        length += len(part) + 1
        taken += 1
    if taken == len(sentences):
        return text, ""
    spoken = " ".join(head)
    if not spoken.rstrip().endswith((".", "!", "?")):
        spoken = spoken.rstrip(".!,? ") + "."
    return spoken, " ".join(sentences[taken:])


# Legacy fixed pointer pool: used ONLY by the outage fallback above (the# model is down so it cannot write a fresh pointer) and kept importable for
# backward compatibility. The live path generates its pointer per question.
# Sir-stacking is avoided: when the spoken line already says "sir", a
# sir-free variant is picked. has_screen=False callers skip it entirely.
OFFERS_WITH_SIR = (
    " The full detail is on screen, sir.",
    " You'll find everything on screen, sir.",
    " The rest is waiting on screen, sir.",
)
OFFERS_PLAIN = (
    " The full detail is on screen.",
    " You'll find everything on screen.",
    " The rest is waiting on screen.",
)

# Backward-compat: the old fixed constant (kept, no longer used by session.py).
DETAIL_OFFER = OFFERS_WITH_SIR[0]


def detail_offer(spoken_text: str | None, rng=None) -> str:
    """Pick the compressed-reply pointer for already-spoken text.

    Never raises; returns "" only when given nothing to judge (treated as
    sir-free). Random rotation keeps repeated long answers from sounding
    canned; the sir check keeps "sir, ... sir" stacking out.
    """
    try:
        pool = OFFERS_PLAIN if "sir" in str(spoken_text or "").lower() else OFFERS_WITH_SIR
        picker = rng if rng is not None else random
        return picker.choice(pool)
    except Exception:
        return OFFERS_WITH_SIR[0]


def level_for(user_text: str = "", taste: str = "auto") -> str:
    """Speech level for a question + standing brevity taste. Never raises.

    Shared by SpeechDistiller.speech_level and the single-shot voice
    instruction so the tag path and the distill path size speech identically.
    """
    try:
        level = score_question(user_text).level
    except Exception:
        level = "normal"
    try:
        taste = (taste or "auto").lower()
    except Exception:
        taste = "auto"
    if taste == "brief":
        return "short"
    if taste == "full" and level == "short":
        return "normal"
    return level if level in SPEECH_BUDGETS else "normal"


def spoken_instruction(user_text: str = "", *, address: str = "sir", brevity: str = "auto") -> str:
    """System line asking the brain for a <spoken> tag, sized like answers.

    Carries the user's taste (address mode, brevity) and the question's
    speech budget so the single-shot tag matches what the distiller would
    have written — including the fresh-pointer and no-repeat rules. The
    static VOICE_SPOKEN_INSTRUCTION in core/orchestrator.py remains as the
    never-break fallback. Never raises.
    """
    try:
        level = level_for(user_text, brevity)
        sentences, words, _ = SPEECH_BUDGETS.get(level, SPEECH_BUDGETS["normal"])
        if address == "none":
            addr = 'Never say "sir" or any other title.'
        elif address and address != "sir":
            addr = f'Address the user as "{address}" at most once. Never say "sir".'
        else:
            addr = 'You may address the user as "sir" at most once.'
        return (
            "You are in a live voice conversation. Provide your response normally, but at the end "
            f"include a <spoken>...</spoken> tag with what to say aloud: at most {sentences} "
            f"sentence(s), under {words} words, leading with the direct answer. {addr} "
            "No markdown, no code, no URLs inside the tag. Never drop numbers, names, or times. "
            "If the spoken line leaves out anything important, end the tag with ONE fresh short "
            "clause naming what is on screen; if it stands alone, end cleanly with no pointer."
        )
    except Exception:
        return (
            "You are in a live voice conversation. Provide your response normally, but at the end "
            "include a <spoken>...</spoken> tag containing a brief, natural spoken summary."
        )


class SpokenTagFilter:
    """Incremental display filter that hides <spoken>...</spoken> from screens.

    Voice generations carry a single-shot spoken tag for TTS; the screen must
    show only the written reply. Chunks stream through feed(); anything inside
    the tag is swallowed (even split across chunks). flush() ends the stream:
    a dangling partial opener and — if the model never closed the tag — the
    hidden text are emitted so nothing the user should see is lost. A runaway
    unclosed tag is capped: past 2000 hidden chars the excess passes through
    as normal text. Case-insensitive, never raises.
    """

    _OPEN = "<spoken"
    _CLOSE = "</spoken>"
    _CAP = 2000

    def __init__(self):
        self._in_tag = False
        self._hidden = ""
        self._carry = ""

    def feed(self, chunk) -> str:
        try:
            data = f"{self._carry}{chunk or ''}"
            self._carry = ""
            out: list[str] = []
            while True:
                low = data.lower()
                if not self._in_tag:
                    i = low.find(self._OPEN)
                    if i == -1:
                        keep = self._prefix_hold(data)
                        out.append(data[: len(data) - keep])
                        self._carry = data[len(data) - keep :]
                        break
                    out.append(data[:i])
                    data = data[i:]
                    self._in_tag = True
                else:
                    j = low.find(self._CLOSE)
                    if j == -1:
                        self._hidden += data
                        if len(self._hidden) > self._CAP:
                            out.append(self._hidden[: len(self._hidden) - self._CAP])
                            self._hidden = self._hidden[-self._CAP :]
                        break
                    data = data[j + len(self._CLOSE) :]
                    self._hidden = ""
                    self._in_tag = False
            return "".join(out)
        except Exception:
            try:
                return str(chunk or "")
            except Exception:
                return ""

    def flush(self) -> str:
        """End of stream: release held text. Never raises."""
        try:
            tail = self._carry
            self._carry = ""
            if self._in_tag and self._hidden:
                # The model never closed the tag: show what it wrote, minus
                # the dangling opener itself.
                hidden = re.sub(r"^<spoken\s*>?\s*", "", self._hidden, flags=re.IGNORECASE)
                tail += hidden
                self._hidden = ""
                self._in_tag = False
            return tail
        except Exception:
            return ""

    @classmethod
    def _prefix_hold(cls, data: str) -> int:
        """Chars to withhold: longest tail that could open a tag."""
        try:
            low = data.lower()
            top = min(len(low), len(cls._OPEN) - 1)
            for k in range(top, 0, -1):
                if low.endswith(cls._OPEN[:k]):
                    return k
            return 0
        except Exception:
            return 0
