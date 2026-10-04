"""Turns a token stream into speakable chunks so TTS can start before the LLM finishes."""
from __future__ import annotations

import re

# Never treat these as sentence ends ("Dr. Reyes", "Suite No. 3").
HARD_ABBREV = {"dr", "mr", "mrs", "ms", "st", "jr", "sr", "vs", "ste", "no", "e.g", "i.e", "approx", "dept"}
_BOUNDARY = re.compile(r"([.!?]+)([\"')\]]*)\s+(?=\S)")
_MARKDOWN = re.compile(r"[*#_`>|]+")


def clean_for_tts(text: str) -> str:
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class SentenceChunker:
    """Emit complete sentences; fall back to clause splits when a sentence runs long.

    The first chunk uses a lower soft limit so the caller hears audio sooner.
    """

    def __init__(self, first_soft_limit: int = 70, soft_limit: int = 170):
        self.buf = ""
        self.first_soft_limit = first_soft_limit
        self.soft_limit = soft_limit
        self.emitted = 0

    def push(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while (seg := self._take_sentence()) is not None:
            out.append(seg)
        limit = self.first_soft_limit if self.emitted == 0 else self.soft_limit
        if len(self.buf) > limit:
            idx = max(self.buf.rfind(", "), self.buf.rfind("; "), self.buf.rfind(" - "))
            if idx > 25:
                out.append(self._emit(self.buf[: idx + 1]))
                self.buf = self.buf[idx + 1:]
        return [s for s in out if s]

    def flush(self) -> list[str]:
        seg = self._emit(self.buf)
        self.buf = ""
        return [seg] if seg else []

    def _emit(self, seg: str) -> str:
        seg = clean_for_tts(seg)
        if seg:
            self.emitted += 1
        return seg

    def _take_sentence(self) -> str | None:
        for m in _BOUNDARY.finditer(self.buf):
            punct = m.group(1)
            nxt = self.buf[m.end()] if m.end() < len(self.buf) else ""
            if punct == ".":
                prev = re.search(r"(\S+)$", self.buf[: m.start()])
                word = prev.group(1).lower().strip("(\"'") if prev else ""
                if word in HARD_ABBREV:
                    continue
                if nxt.islower():  # "2 p.m. with Dr. Reyes" -> keep going
                    continue
                if re.fullmatch(r"\d+", word) and nxt.isdigit():
                    continue
            seg = self.buf[: m.end()]
            self.buf = self.buf[m.end():]
            return self._emit(seg)
        return None
