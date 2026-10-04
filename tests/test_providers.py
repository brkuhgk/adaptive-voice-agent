"""Exercise the real provider clients against local fake servers that speak each wire protocol."""
import asyncio
import json
import socket
from urllib.parse import parse_qs, urlparse

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse

from app.config import Settings
from app.llm import OpenAICompatibleLLM
from app.stt import DeepgramSTT
import app.tts as tts_mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, app):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, port=self.port, log_level="warning", lifespan="off"))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self.task


def sse_chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


async def test_openai_compatible_stream_parses_text_and_tool_calls(monkeypatch):
    seen = {}
    app = FastAPI()

    @app.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        seen["body"], seen["headers"] = body, dict(request.headers)
        if body.get("response_format"):
            return {"id": "y", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": '{"intent": "cancel"}'}}]}

        async def gen():
            yield sse_chunk({"role": "assistant", "content": "One sec."})
            yield sse_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                             "function": {"name": "check_availability", "arguments": ""}}]})
            yield sse_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"visit_type":'}}]})
            yield sse_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"sick_visit"}'}}]})
            yield sse_chunk({}, finish="tool_calls")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    async with Server(app) as srv:
        settings = Settings()
        settings.llm_provider = "foundry"
        settings.llm_api_key = "k-123"
        settings.llm_base_url = f"http://127.0.0.1:{srv.port}/openai/v1/"
        settings.llm_model = "gpt-4.1-mini"
        llm = OpenAICompatibleLLM(settings)
        deltas = [d async for d in llm.stream_chat([{"role": "user", "content": "hi"}], tools=[{"type": "function",
                  "function": {"name": "check_availability", "parameters": {"type": "object", "properties": {}}}}])]
        text = "".join(d.text for d in deltas if d.text)
        args = "".join(d.tool_args or "" for d in deltas if d.tool_index == 0)
        assert text == "One sec."
        assert [d.tool_name for d in deltas if d.tool_name] == ["check_availability"]
        assert json.loads(args) == {"visit_type": "sick_visit"}
        assert deltas[-1].finish_reason == "tool_calls"
        assert seen["body"]["model"] == "gpt-4.1-mini" and seen["body"]["stream"] is True
        assert seen["headers"]["api-key"] == "k-123"  # Foundry key header
        assert await llm.complete_json([{"role": "user", "content": "x"}]) == {"intent": "cancel"}


async def test_elevenlabs_streams_ulaw_and_caches_short_phrases(monkeypatch):
    calls = []
    app = FastAPI()

    @app.post("/v1/text-to-speech/{voice_id}/stream")
    async def tts(voice_id: str, request: Request):
        calls.append({"voice": voice_id, "query": dict(request.query_params), "key": request.headers.get("xi-api-key"),
                      "body": await request.json()})

        async def gen():
            for _ in range(3):
                yield b"\x7f" * 400
        return StreamingResponse(gen(), media_type="audio/basic")

    async with Server(app) as srv:
        monkeypatch.setattr(tts_mod, "ELEVEN_URL", f"http://127.0.0.1:{srv.port}/v1/text-to-speech/{{voice_id}}/stream")
        settings = Settings()
        settings.elevenlabs_api_key = "el-key"
        client = tts_mod.ElevenLabsTTS(settings)
        audio = b"".join([c async for c in client.stream("Hello there.")])
        again = b"".join([c async for c in client.stream("Hello there.")])
        await client.aclose()
    assert audio == again == b"\x7f" * 1200
    assert len(calls) == 1  # second time came from cache
    assert calls[0]["query"]["output_format"] == "ulaw_8000"
    assert calls[0]["key"] == "el-key" and calls[0]["body"]["model_id"] == settings.elevenlabs_model


async def test_deepgram_turn_assembly_over_websocket():
    seen = {}
    app = FastAPI()

    def results(text, is_final, speech_final=False):
        return json.dumps({"type": "Results", "is_final": is_final, "speech_final": speech_final,
                           "channel": {"alternatives": [{"transcript": text}]}})

    @app.websocket("/v1/listen")
    async def listen(ws: WebSocket):
        seen["query"] = parse_qs(urlparse(str(ws.url)).query)
        seen["auth"] = ws.headers.get("authorization")
        await ws.accept()
        audio = await ws.receive_bytes()
        seen["audio"] = audio
        await ws.send_text(json.dumps({"type": "SpeechStarted", "channel": [0, 1], "timestamp": 0.2}))
        await ws.send_text(results("I need to", False))
        await ws.send_text(results("I need to book", True))           # final segment, speaker continues
        await ws.send_text(results("for my daughter", True, speech_final=True))  # turn ends
        await ws.send_text(results("Her name is", True))
        await ws.send_text(json.dumps({"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": 3.1}))
        await ws.send_text(json.dumps({"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": -1}))  # no-op
        try:
            while True:
                await ws.receive_text()
        except Exception:
            pass

    async with Server(app) as srv:
        settings = Settings()
        settings.deepgram_api_key = "dg-key"
        stt = DeepgramSTT(settings, keyterms=["Okafor", "Riverbend"])
        stt.URL = f"ws://127.0.0.1:{srv.port}/v1/listen"
        await stt.start()
        await stt.send_audio(b"\xff" * 160)
        events = []
        while len([e for e in events if e.kind == "final"]) < 2:
            events.append(await asyncio.wait_for(stt.events.get(), 3))
        await stt.close()

    finals = [e.text for e in events if e.kind == "final"]
    assert finals == ["I need to book for my daughter", "Her name is"]
    assert events[0].kind == "speech_started"
    assert any(e.kind == "interim" and e.text == "I need to" for e in events)
    assert seen["auth"] == "Token dg-key" and seen["audio"] == b"\xff" * 160
    q = seen["query"]
    assert q["encoding"] == ["mulaw"] and q["sample_rate"] == ["8000"] and q["model"] == ["nova-3"]
    assert q["keyterm"] == ["Okafor", "Riverbend"] and q["vad_events"] == ["true"]
