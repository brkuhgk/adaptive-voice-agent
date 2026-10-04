"""Runtime configuration, loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# Default base URLs for the OpenAI-compatible providers we support.
LLM_PRESETS = {
    # Microsoft Foundry / Azure OpenAI "v1" API. Replace <resource> or set LLM_BASE_URL.
    "foundry": "https://<resource>.openai.azure.com/openai/v1/",
    # GitHub Models: free, rate-limited access to the Foundry model catalog (great for dev).
    "github": "https://models.github.ai/inference",
    # Plain OpenAI.
    "openai": "https://api.openai.com/v1",
}

DEFAULT_MODELS = {
    "foundry": "gpt-4.1-mini",          # your *deployment* name in Foundry
    "github": "openai/gpt-4.1-mini",
    "openai": "gpt-4.1-mini",
}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    # --- LLM -------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "foundry").lower())
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.6")))
    # The state tracker is a second, small LLM call that runs in parallel with the
    # spoken reply and extracts structured state. Turn it off to halve LLM calls
    # (useful on GitHub Models' free rate limits).
    state_tracker_enabled: bool = field(default_factory=lambda: _bool("STATE_TRACKER_ENABLED", True))
    max_history_messages: int = field(default_factory=lambda: _int("MAX_HISTORY_MESSAGES", 24))

    # --- Deepgram (STT) --------------------------------------------------
    deepgram_api_key: str = field(default_factory=lambda: os.getenv("DEEPGRAM_API_KEY", ""))
    deepgram_model: str = field(default_factory=lambda: os.getenv("DEEPGRAM_MODEL", "nova-3"))
    deepgram_language: str = field(default_factory=lambda: os.getenv("DEEPGRAM_LANGUAGE", "en-US"))
    endpointing_ms: int = field(default_factory=lambda: _int("DEEPGRAM_ENDPOINTING_MS", 400))
    utterance_end_ms: int = field(default_factory=lambda: _int("DEEPGRAM_UTTERANCE_END_MS", 1000))

    # --- ElevenLabs (TTS) ------------------------------------------------
    elevenlabs_api_key: str = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY", ""))
    # "Sarah" premade voice; run scripts/check_setup.py to list voices on your account.
    elevenlabs_voice_id: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"))
    elevenlabs_model: str = field(default_factory=lambda: os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"))

    # --- Twilio ----------------------------------------------------------
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", ""))
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", ""))
    twilio_phone_number: str = field(default_factory=lambda: os.getenv("TWILIO_PHONE_NUMBER", ""))
    validate_twilio_signature: bool = field(default_factory=lambda: _bool("TWILIO_VALIDATE_SIGNATURE", False))
    # Outbound calls are only placed to numbers on this comma-separated allowlist
    # (people who agreed to take part in the demo).
    outbound_allowlist: list[str] = field(
        default_factory=lambda: [n.strip() for n in os.getenv("OUTBOUND_ALLOWLIST", "").split(",") if n.strip()]
    )

    # --- Follow-up messages (sent during campaign calls) -----------------
    # SMS: needs a paid Twilio account plus Toll-Free Verification or A2P 10DLC for US numbers.
    sms_enabled: bool = field(default_factory=lambda: _bool("SMS_ENABLED", False))  # true once Twilio verified you
    sms_from: str = field(default_factory=lambda: os.getenv("TWILIO_SMS_FROM", os.getenv("TWILIO_PHONE_NUMBER", "")))
    twilio_messaging_service_sid: str = field(default_factory=lambda: os.getenv("TWILIO_MESSAGING_SERVICE_SID", ""))
    # Email: "sendgrid" (Twilio SendGrid API) or "smtp" (for example Gmail with an app password).
    email_provider: str = field(default_factory=lambda: os.getenv("EMAIL_PROVIDER", "").lower())
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))
    email_from_name: str = field(default_factory=lambda: os.getenv("EMAIL_FROM_NAME", ""))
    sendgrid_api_key: str = field(default_factory=lambda: os.getenv("SENDGRID_API_KEY", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))

    # --- Server ----------------------------------------------------------
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    scenario: str = field(default_factory=lambda: os.getenv("SCENARIO", "clinic"))
    # Where results.csv, transcripts and the do-not-call list are written (a volume in the cloud).
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", ""))
    stream_secret: str = field(default_factory=lambda: os.getenv("STREAM_SECRET", "change-me-for-the-demo"))
    dashboard_token: str = field(default_factory=lambda: os.getenv("DASHBOARD_TOKEN", ""))

    # --- Conversation behaviour -----------------------------------------
    barge_in_min_chars: int = field(default_factory=lambda: _int("BARGE_IN_MIN_CHARS", 4))
    silence_timeout_s: int = field(default_factory=lambda: _int("SILENCE_TIMEOUT_S", 9))

    @property
    def resolved_llm_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        return LLM_PRESETS.get(self.llm_provider, LLM_PRESETS["openai"])

    @property
    def resolved_llm_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "gpt-4.1-mini")

    @property
    def is_public(self) -> bool:
        """True when the server is reachable from the internet (cloud host or tunnel)."""
        url = self.public_base_url
        return bool(url) and not any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    @property
    def public_ws_base(self) -> str:
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")


settings = Settings()
