"""Speech-to-text. Deepgram live streaming on the raw 8 kHz mu-law audio Twilio sends us.

Turn detection: collect `is_final` segments; a turn ends on `speech_final` (endpointing
silence) or on `UtteranceEnd` (word-gap fallback that still works with background noise).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class STTEvent:
    kind: str  # "interim" | "final" | "speech_started" | "error" | "closed"
    text: str = ""


class STTClient(Protocol):
    events: asyncio.Queue

    async def start(self) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def close(self) -> None: ...


class DeepgramSTT:
    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, settings: Settings, keyterms: list[str] | None = None):
        if not settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set.")
        self.settings = settings
        self.keyterms = keyterms or []
        self.events: asyncio.Queue[STTEvent] = asyncio.Queue()
        self._ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task] = []
        self._segments: list[str] = []
        self._last_audio = time.monotonic()
        self._closing = False

    def _url(self) -> str:
        params: list[tuple[str, str]] = [
            ("model", self.settings.deepgram_model),
            ("language", self.settings.deepgram_language),
            ("encoding", "mulaw"),
            ("sample_rate", "8000"),
            ("channels", "1"),
            ("interim_results", "true"),
            ("endpointing", str(self.settings.endpointing_ms)),
            ("utterance_end_ms", str(max(1000, self.settings.utterance_end_ms))),
            ("vad_events", "true"),
            ("smart_format", "true"),
            ("punctuate", "true"),
        ]
        # Keyterm prompting (nova-3) boosts names like provider names and the clinic name.
        if self.settings.deepgram_model.startswith("nova-3"):
            params += [("keyterm", k) for k in self.keyterms[:20]]
        return f"{self.URL}?{urlencode(params)}"

    async def start(self) -> None:
        self._ws = await connect(self._url(), additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"},
                                 max_size=None, ping_interval=10)
        self._tasks = [asyncio.create_task(self._recv_loop()), asyncio.create_task(self._keepalive_loop())]

    async def send_audio(self, audio: bytes) -> None:
        if self._ws is None:
            return
        self._last_audio = time.monotonic()
        try:
            await self._ws.send(audio)
        except Exception as exc:
            log.warning("deepgram send failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(4)
            if self._ws is not None and time.monotonic() - self._last_audio > 4:
                await self._ws.send(json.dumps({"type": "KeepAlive"}))

    def _emit_turn(self) -> None:
        text = " ".join(s for s in self._segments if s).strip()
        self._segments.clear()
        if text:
            self.events.put_nowait(STTEvent("final", text))

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "Results":
                    alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                    transcript = (alt.get("transcript") or "").strip()
                    if msg.get("is_final"):
                        if transcript:
                            self._segments.append(transcript)
                            self.events.put_nowait(STTEvent("interim", " ".join(self._segments)))
                        if msg.get("speech_final"):
                            self._emit_turn()
                    elif transcript:
                        self.events.put_nowait(STTEvent("interim", " ".join([*self._segments, transcript])))
                elif mtype == "UtteranceEnd":
                    self._emit_turn()
                elif mtype == "SpeechStarted":
                    self.events.put_nowait(STTEvent("speech_started"))
                elif mtype == "Error" or msg.get("err_code"):
                    log.error("deepgram error: %s", msg)
                    self.events.put_nowait(STTEvent("error", json.dumps(msg)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("deepgram receive loop ended: %s", exc)
        if not self._closing:  # dropped by the server/network, not by us
            self.events.put_nowait(STTEvent("closed"))

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.wait_for(self._ws.close(), timeout=2)
            except Exception:
                pass
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None


class FakeSTT:
    """Test double. Audio payloads starting with FINAL:/INTERIM: become transcript events,
    so a fake Twilio client can 'speak' through the real media-stream WebSocket."""

    def __init__(self, keyterms: list[str] | None = None) -> None:
        self.keyterms = keyterms or []
        self.events: asyncio.Queue[STTEvent] = asyncio.Queue()
        self.audio_bytes = 0
        self.closed = False

    async def start(self) -> None:
        return None

    async def send_audio(self, audio: bytes) -> None:
        if audio == b"DROP":  # simulate the provider dropping the connection
            self.events.put_nowait(STTEvent("closed"))
            return
        for prefix, kind in ((b"FINAL:", "final"), (b"INTERIM:", "interim")):
            if audio.startswith(prefix):
                self.events.put_nowait(STTEvent(kind, audio[len(prefix):].decode()))
                return
        self.audio_bytes += len(audio)

    async def close(self) -> None:
        self.closed = True
