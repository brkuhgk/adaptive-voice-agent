"""Text-to-speech. ElevenLabs streams 8 kHz mu-law, which Twilio plays with no conversion."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"


class TTSError(RuntimeError):
    pass


class TTSClient(Protocol):
    def stream(self, text: str, previous_text: str | None = None, voice_id: str | None = None
               ) -> AsyncIterator[bytes]: ...


class ElevenLabsTTS:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set.")
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._cache: dict[tuple[str, str], bytes] = {}

    async def stream(self, text: str, previous_text: str | None = None, voice_id: str | None = None
                     ) -> AsyncIterator[bytes]:
        voice = voice_id or self.settings.elevenlabs_voice_id
        cached = self._cache.get((voice, text))
        if cached is not None:
            for i in range(0, len(cached), 3200):
                yield cached[i:i + 3200]
            return

        url = ELEVEN_URL.format(voice_id=voice)
        body = {
            "text": text,
            "model_id": self.settings.elevenlabs_model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "speed": 1.05},
        }
        if previous_text:
            body["previous_text"] = previous_text[-300:]
        collected = bytearray()
        async with self.client.stream(
            "POST", url, params={"output_format": "ulaw_8000"},
            headers={"xi-api-key": self.settings.elevenlabs_api_key, "Content-Type": "application/json"},
            json=body,
        ) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode(errors="ignore")[:300]
                raise TTSError(f"ElevenLabs {resp.status_code}: {detail}")
            async for chunk in resp.aiter_bytes():
                if chunk:
                    collected.extend(chunk)
                    yield chunk
        # Short, frequently repeated phrases (greeting, fillers) get cached.
        if len(text) <= 160 and not previous_text:
            if len(self._cache) > 300:
                self._cache.pop(next(iter(self._cache)))
            self._cache[(voice, text)] = bytes(collected)

    async def warm(self, text: str, voice_id: str | None = None) -> None:
        """Pre-synthesize a line (split exactly like the call does) so it plays instantly."""
        from .sentence import SentenceChunker

        chunker = SentenceChunker()
        for part in [*chunker.push(text), *chunker.flush()]:
            try:
                async for _ in self.stream(part, voice_id=voice_id):
                    pass
            except Exception as exc:
                log.warning("TTS warm-up failed for %r: %s", part, exc)

    async def list_voices(self) -> list[dict]:
        r = await self.client.get("https://api.elevenlabs.io/v1/voices",
                                  headers={"xi-api-key": self.settings.elevenlabs_api_key})
        r.raise_for_status()
        return [{"voice_id": v["voice_id"], "name": v.get("name"), "category": v.get("category"),
                 "labels": v.get("labels") or {}, "preview_url": v.get("preview_url")}
                for v in r.json().get("voices", [])]

    async def aclose(self) -> None:
        await self.client.aclose()


class FakeTTS:
    """Emits 'audio' sized like real speech (~13 chars/sec at 8000 bytes/sec)."""

    def __init__(self, chunk_delay: float = 0.0):
        self.chunk_delay = chunk_delay
        self.spoken: list[str] = []
        self.voices_used: list[str | None] = []

    async def stream(self, text: str, previous_text: str | None = None, voice_id: str | None = None
                     ) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        self.voices_used.append(voice_id)
        total = max(800, len(text) * 600)
        for i in range(0, total, 1600):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            yield b"\xff" * min(1600, total - i)
