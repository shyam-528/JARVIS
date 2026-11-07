"""Config loading — config.yaml + .env into typed dataclasses.

Owns: every tunable in the system (providers, voice, telegram, API, tray/HUD,
summarizer, persona, dialogue, preferences...) and the KeyReport that tells
the user which providers are active vs skipped for missing keys.

Rules:
- A malformed config must fail LOUDLY and clearly at boot (ValueError naming
  the offending section), never silently misconfigure a running system.
- A missing API key is NOT an error: the provider is skipped and reported in
  KeyReport.missing_env so the chain degrades gracefully.
- Defaults here are the product: every field must boot on an empty
  config.yaml. Never add a required field without a default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str
    priority: int = 0
    daily_token_budget: int = 1_000_000
    first_token_timeout_s: float | None = None
    supports_tools: bool = False
    supports_chat: bool = True
    preferred: bool = False
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    # Health tracking (runtime)
    _success_count: int = field(default=0, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _last_success: float = field(default=0.0, init=False, repr=False)
    _last_failure: float = field(default=0.0, init=False, repr=False)


@dataclass
class WatchdogConfig:
    first_token_timeout_s: float = 1.5
    connect_timeout_s: float = 8.0
    read_timeout_s: float = 30.0


@dataclass
class VoiceConfig:
    whisper_model: str = "small"
    stt_language: str | None = None
    # Voice pairing for consistent character across engines:
    # Gemini "Kore" <-> edge "SoniaNeural" (female British register)
    tts_voice: str = "en-GB-SoniaNeural"
    piper_voice: str = "en_GB-alan-medium"
    # auto = gemini (expressive) -> edge -> piper -> sapi
    tts_engine: str = "auto"
    tts_enabled: bool = True
    silence_timeout_s: float = 0.9
    max_record_s: float = 15.0
    speak_max_chars: int = 300
    wake_threshold: float = 0.5
    distill_speech: bool = True
    distill_threshold: int = 200
    # Speech-writer style: persona "butler" (movie-JARVIS) or "plain";
    # address "sir", "none", or a name. Runtime corrections ("don't call
    # me sir") persist in data/voice_prefs.json and win over these seeds.
    distill_persona: str = "butler"
    distill_address: str = "sir"
    # Gemini TTS: voice name + butler persona style directions
    gemini_voice: str = "Kore"
    tts_persona: str = ""
    # Unlock greeting (core/session_greeting.py): speak "welcome back, sir"
    # on every workstation unlock, rate-limited; quiet hours suppress speech.
    greeting_enabled: bool = True
    greeting_min_interval_min: float = 60.0
    greeting_quiet_hours: str = "22-7"
    # Barge-in: mic watches during playback; sustained speech interrupts.
    barge_in: bool = False
    barge_in_ms: int = 400
    # Spoken interjections: finished bg jobs announce themselves aloud.
    interject_jobs: bool = True
    # Always-on wake word (step A): "Hey Jarvis" listener runs in the
    # background alongside the terminal from boot.
    wake_autostart: bool = True


@dataclass
class NotificationConfig:
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_auth_token: str = ""
    pushover_user_key: str = ""
    pushover_app_token: str = ""
    webhook_url: str = ""
    webhook_method: str = "POST"
    whatsapp_phone: str = ""
    whatsapp_apikey: str = ""
    default_priority: str = "default"
    default_channel: str = "telegram"
    default_tags: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class CodeAgentConfig:
    auto_approve: bool = True
    blocked_commands: list[str] = field(
        default_factory=lambda: [
            "rm -rf /",
            "format c:",
            "shutdown",
            "del /s /q",
            "rmdir /s /q",
            "diskpart",
            "bcdedit",
        ]
    )
    max_file_size_mb: float = 10.0
    timeout_s: int = 120


@dataclass
class A2AConfig:
    enabled: bool = True
    port: int = 8082
    host: str = "0.0.0.0"
    auto_discovery: bool = True
    gossip_interval: int = 30
    api_key: str = ""
    allowed_origins: list[str] = field(default_factory=list)
    require_auth: bool = True


@dataclass
class PluginSecurityConfig:
    enabled: bool = True
    require_signature: bool = True
    allowed_keys: list[str] = field(default_factory=list)
    hash_pinning: bool = True
    allowed_hashes: list[str] = field(default_factory=list)


@dataclass
class RateLimitConfig:
    enabled: bool = True
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst: int = 10


@dataclass
class APIConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:8080", "http://127.0.0.1:8080"]
    )
    token_expiry_days: int = 30
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class ProactiveConfig:
    enabled: bool = True
    suggestion_threshold: float = 0.7
    detect_recurring_topics: bool = True
    # auto_act: high-confidence suggestions with actions EXECUTE instead of
    # just printing. Default off (opt-in). Threshold 0.9, notify tools only.
    auto_act: bool = False


@dataclass
class TrayConfig:
    enabled: bool = True
    hotkey: str = "ctrl+alt+j"
    show_on_startup: bool = False
    hud: bool = True  # desktop HUD overlay (hud.py) launched with the CLI


@dataclass
class ComputerUseConfig:
    enabled: bool = True
    failsafe: bool = True
    pause: float = 0.05
    max_screenshots: int = 50


@dataclass
class SummarizerConfig:
    enabled: bool = True
    model: str = "gemini-3.5-flash"
    max_summary_tokens: int = 2000
    trigger_ratio: float = 0.8
    min_turns_to_summarize: int = 10


@dataclass
class PersonaConfig:
    default: str = "professional"


@dataclass
class DialogueConfig:
    enabled: bool = True
    max_threads: int = 5
    max_history: int = 100


@dataclass
class PreferenceConfig:
    enabled: bool = True
    auto_learn: bool = True
    min_confidence: float = 0.7


@dataclass
class Config:
    jarvis_name: str
    system_prompt: str
    watchdog: WatchdogConfig
    providers: list[ProviderConfig]
    db_path: Path
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    notify: NotificationConfig = field(default_factory=NotificationConfig)
    a2a: A2AConfig = field(default_factory=A2AConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    code_agent: CodeAgentConfig = field(default_factory=CodeAgentConfig)
    tray: TrayConfig = field(default_factory=TrayConfig)
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)
    api: APIConfig = field(default_factory=APIConfig)
    plugin_security: PluginSecurityConfig = field(default_factory=PluginSecurityConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    dialogue: DialogueConfig = field(default_factory=DialogueConfig)
    preferences: PreferenceConfig = field(default_factory=PreferenceConfig)


class KeyReport(NamedTuple):
    active: list[str]
    missing_env: list[str]
    disabled: list[str]


def _watchdog_from(raw: dict) -> WatchdogConfig:
    w = raw.get("watchdog", {}) or {}
    return WatchdogConfig(
        first_token_timeout_s=float(w.get("first_token_timeout_ms", 1500)) / 1000.0,
        connect_timeout_s=float(w.get("connect_timeout_ms", 8000)) / 1000.0,
        read_timeout_s=float(w.get("read_timeout_ms", 30000)) / 1000.0,
    )


def _voice_from(raw: dict) -> VoiceConfig:
    v = raw.get("voice", {}) or {}
    lang = v.get("stt_language")
    speech = v.get("speech", {}) or {}
    return VoiceConfig(
        whisper_model=str(v.get("whisper_model", "small")),
        stt_language=str(lang) if lang else None,
        tts_voice=str(v.get("tts_voice", "en-GB-SoniaNeural")),
        piper_voice=str(v.get("piper_voice", "en_GB-alan-medium")),
        tts_engine=str(v.get("tts_engine", "auto")),
        tts_enabled=bool(v.get("tts_enabled", True)),
        silence_timeout_s=float(v.get("silence_timeout_s", 0.9)),
        max_record_s=float(v.get("max_record_s", 15.0)),
        speak_max_chars=int(v.get("speak_max_chars", 300)),
        wake_threshold=float(v.get("wake_threshold", 0.5)),
        distill_speech=bool(speech.get("distill", v.get("distill_speech", True))),
        distill_threshold=int(speech.get("distill_threshold", v.get("distill_threshold", 200))),
        distill_persona=str(speech.get("persona", v.get("distill_persona", "butler")) or "butler"),
        distill_address=str(speech.get("address", v.get("distill_address", "sir")) or "sir"),
        gemini_voice=str(v.get("gemini_voice", "Kore")),
        tts_persona=str(v.get("tts_persona", "")),
        greeting_enabled=bool(v.get("greeting_enabled", True)),
        greeting_min_interval_min=float(v.get("greeting_min_interval_min", 60.0)),
        greeting_quiet_hours=str(v.get("greeting_quiet_hours", "22-7")),
        barge_in=bool(v.get("barge_in", False)),
        barge_in_ms=int(v.get("barge_in_ms", 400)),
        interject_jobs=bool(v.get("interject_jobs", True)),
        wake_autostart=bool(v.get("wake_autostart", True)),
    )


def _notify_from(raw: dict) -> NotificationConfig:
    import os as _os

    n = raw.get("notify", {}) or {}
    return NotificationConfig(
        ntfy_server=str(n.get("ntfy_server", "https://ntfy.sh")),
        ntfy_topic=str(n.get("ntfy_topic", "")),
        ntfy_auth_token=str(n.get("ntfy_auth_token", "")),
        pushover_user_key=str(n.get("pushover_user_key", "")),
        pushover_app_token=str(n.get("pushover_app_token", "")),
        webhook_url=str(n.get("webhook_url", "")),
        webhook_method=str(n.get("webhook_method", "POST")),
        whatsapp_phone=_os.getenv("WHATSAPP_PHONE", "").strip() or str(n.get("whatsapp_phone", "")),
        whatsapp_apikey=_os.getenv("WHATSAPP_APIKEY", "").strip()
        or str(n.get("whatsapp_apikey", "")),
        default_priority=str(n.get("default_priority", "default")),
        default_channel=str(n.get("default_channel", "ntfy")),
        default_tags=list(n.get("default_tags", [])),
    )


def _a2a_from(raw: dict) -> A2AConfig:
    a = raw.get("a2a", {}) or {}
    # Check YAML first, then environment variable
    api_key = str(a.get("api_key", ""))
    if not api_key:
        import os

        api_key = os.getenv("MESH_API_KEY", "")
    return A2AConfig(
        enabled=bool(a.get("enabled", True)),
        port=int(a.get("port", 8082)),
        host=str(a.get("host", "0.0.0.0")),
        auto_discovery=bool(a.get("auto_discovery", True)),
        gossip_interval=int(a.get("gossip_interval", 30)),
        api_key=api_key,
        allowed_origins=list(a.get("allowed_origins", [])),
        require_auth=bool(a.get("require_auth", True)),
    )


def _api_from(raw: dict) -> APIConfig:
    a = raw.get("api", {}) or {}
    return APIConfig(
        host=str(a.get("host", "0.0.0.0")),
        port=int(a.get("port", 8080)),
        cors_origins=list(
            a.get("cors_origins", ["http://localhost:8080", "http://127.0.0.1:8080"])
        ),
        token_expiry_days=int(a.get("token_expiry_days", 30)),
        rate_limit=_rate_limit_from(a.get("rate_limit", {})),
    )


def _rate_limit_from(raw: dict) -> RateLimitConfig:
    return RateLimitConfig(
        enabled=bool(raw.get("enabled", True)),
        requests_per_minute=int(raw.get("requests_per_minute", 60)),
        requests_per_hour=int(raw.get("requests_per_hour", 1000)),
        burst=int(raw.get("burst", 10)),
    )


def _plugin_security_from(raw: dict) -> PluginSecurityConfig:
    p = raw.get("plugin_security", {}) or {}
    return PluginSecurityConfig(
        enabled=bool(p.get("enabled", True)),
        require_signature=bool(p.get("require_signature", True)),
        allowed_keys=list(p.get("allowed_keys", [])),
        hash_pinning=bool(p.get("hash_pinning", True)),
        allowed_hashes=list(p.get("allowed_hashes", [])),
    )


def _proactive_from(raw: dict) -> ProactiveConfig:
    p = raw.get("proactive", {}) or {}
    return ProactiveConfig(
        enabled=bool(p.get("enabled", True)),
        suggestion_threshold=float(p.get("suggestion_threshold", 0.7)),
        detect_recurring_topics=bool(p.get("detect_recurring_topics", True)),
        auto_act=bool(p.get("auto_act", False)),
    )


def _telegram_from(raw: dict) -> TelegramConfig:
    t = raw.get("telegram", {}) or {}
    # P0: env wins over YAML so secrets never need to live in config.yaml.
    # Supports TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (see .env.example).
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or str(t.get("bot_token", ""))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or str(t.get("chat_id", ""))
    return TelegramConfig(
        enabled=bool(t.get("enabled", False)),
        bot_token=bot_token,
        chat_id=chat_id,
    )


def _code_agent_from(raw: dict) -> CodeAgentConfig:
    c = raw.get("code_agent", {}) or {}
    return CodeAgentConfig(
        auto_approve=bool(c.get("auto_approve", False)),
        blocked_commands=list(
            c.get(
                "blocked_commands",
                [
                    "rm -rf /",
                    "format c:",
                    "shutdown",
                    "del /s /q",
                    "rmdir /s /q",
                    "diskpart",
                    "bcdedit",
                ],
            )
        ),
        max_file_size_mb=float(c.get("max_file_size_mb", 10.0)),
        timeout_s=int(c.get("timeout_s", 120)),
    )


def _tray_from(raw: dict) -> TrayConfig:
    t = raw.get("tray", {}) or {}
    return TrayConfig(
        enabled=bool(t.get("enabled", True)),
        hotkey=str(t.get("hotkey", "ctrl+alt+j")),
        show_on_startup=bool(t.get("show_on_startup", False)),
        hud=bool(t.get("hud", True)),
    )


def _summarizer_from(raw: dict) -> SummarizerConfig:
    s = raw.get("summarizer", {}) or {}
    return SummarizerConfig(
        enabled=bool(s.get("enabled", True)),
        model=str(s.get("model", "gemini-3.5-flash")),
        max_summary_tokens=int(s.get("max_summary_tokens", 2000)),
        trigger_ratio=float(s.get("trigger_ratio", 0.8)),
        min_turns_to_summarize=int(s.get("min_turns_to_summarize", 10)),
    )


def _persona_from(raw: dict) -> PersonaConfig:
    p = raw.get("persona", {}) or {}
    return PersonaConfig(
        default=str(p.get("default", "professional")),
    )


def _dialogue_from(raw: dict) -> DialogueConfig:
    d = raw.get("dialogue", {}) or {}
    return DialogueConfig(
        enabled=bool(d.get("enabled", True)),
        max_threads=int(d.get("max_threads", 5)),
        max_history=int(d.get("max_history", 100)),
    )


def _preference_from(raw: dict) -> PreferenceConfig:
    p = raw.get("preferences", {}) or {}
    return PreferenceConfig(
        enabled=bool(p.get("enabled", True)),
        auto_learn=bool(p.get("auto_learn", True)),
        min_confidence=float(p.get("min_confidence", 0.7)),
    )


def _computer_use_from(raw: dict) -> ComputerUseConfig:
    c = raw.get("computer_use", {}) or {}
    return ComputerUseConfig(
        enabled=bool(c.get("enabled", True)),
        failsafe=bool(c.get("failsafe", True)),
        pause=float(c.get("pause", 0.05)),
        max_screenshots=int(c.get("max_screenshots", 50)),
    )


def _providers_from(raw: dict) -> tuple[list[ProviderConfig], KeyReport]:
    active: list[ProviderConfig] = []
    missing_env: list[str] = []
    disabled: list[str] = []
    for i, p in enumerate(raw.get("providers", []) or []):
        missing_fields = [f for f in ("name", "key_env", "base_url", "model") if not p.get(f)]
        if missing_fields:
            label = p.get("name") or f"providers[{i}]"
            raise ValueError(
                f"config.yaml provider '{label}' is missing required field(s): "
                f"{', '.join(missing_fields)}"
            )
        name = str(p["name"])  # YAML 1.1: unquoted on/off/yes/no parse as bool
        if not p.get("enabled", True):
            disabled.append(name)
            continue
        key = os.getenv(p["key_env"], "").strip()
        if not key:
            missing_env.append(f"{name} ({p['key_env']})")
            continue
        active.append(
            ProviderConfig(
                name=name,
                base_url=p["base_url"].rstrip("/"),
                model=p["model"],
                api_key=key,
                priority=int(p.get("priority", 0)),
                daily_token_budget=int(p.get("daily_token_budget", 1_000_000)),
                first_token_timeout_s=(
                    float(p["first_token_timeout_ms"]) / 1000.0
                    if p.get("first_token_timeout_ms")
                    else None
                ),
                supports_tools=bool(p.get("supports_tools", False)),
                supports_chat=bool(p.get("supports_chat", True)),
                preferred=bool(p.get("preferred", False)),
                enabled=bool(p.get("enabled", True)),
                tags=list(p.get("tags", [])),
            )
        )
    active.sort(key=lambda x: x.priority)
    return active, KeyReport(
        active=[p.name for p in active], missing_env=missing_env, disabled=disabled
    )


def load_config(config_path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = config_path or ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    providers, _ = _providers_from(raw)
    j = raw.get("jarvis", {}) or {}
    return Config(
        jarvis_name=j.get("name", "JARVIS MARK 3"),
        system_prompt=j.get("system_prompt", "You are JARVIS, a helpful personal AI assistant."),
        watchdog=_watchdog_from(raw),
        providers=providers,
        db_path=ROOT / "data" / "usage.db",
        voice=_voice_from(raw),
        notify=_notify_from(raw),
        a2a=_a2a_from(raw),
        proactive=_proactive_from(raw),
        telegram=_telegram_from(raw),
        code_agent=_code_agent_from(raw),
        tray=_tray_from(raw),
        computer_use=_computer_use_from(raw),
        api=_api_from(raw),
        plugin_security=_plugin_security_from(raw),
        summarizer=_summarizer_from(raw),
        persona=_persona_from(raw),
        dialogue=_dialogue_from(raw),
        preferences=_preference_from(raw),
    )


def key_report(config_path: Path | None = None) -> KeyReport:
    load_dotenv(ROOT / ".env")
    cfg_path = config_path or ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    _, report = _providers_from(raw)
    return report
