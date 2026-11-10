"""Chunker: a streaming generator with a bounded buffer (never the whole text in memory)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str


def _cut_point(buffer: str, size: int) -> int:
    """Prefer to cut at whitespace in the last 20% of the window; otherwise cut hard at ``size``."""
    lower = int(size * 0.8)
    for position in range(size - 1, lower - 1, -1):
        if buffer[position].isspace():
            return position + 1
    return size


def chunk_texts(texts: Iterable[str], size: int, overlap: int) -> Iterator[Chunk]:
    if overlap * 2 >= size:
        raise ValueError("overlap must be smaller than half of the chunk size")

    buffer = ""
    kept = 0  # how many leading characters of the buffer are only overlap from the previous chunk
    index = 0
    for text in texts:
        if not text:
            continue
        buffer = f"{buffer}\n{text}" if buffer else text
        while len(buffer) >= size:
            cut = _cut_point(buffer, size)
            piece = buffer[:cut].strip()
            if piece:
                yield Chunk(index, piece)
                index += 1
            tail = buffer[max(cut - overlap, 0) : cut]
            buffer = tail + buffer[cut:]  # always shorter than before, so the loop terminates
            kept = len(tail)
    if buffer[kept:].strip():  # is there genuinely new text beyond the overlap?
        yield Chunk(index, buffer.strip())