"""Streaming multipart/form-data parser for the upload drop zone (architecture §6.1).

Why not starlette's ``request.form()``: it spools the entire request body to a
temporary file BEFORE the endpoint runs, so the 100 MB hard cap (§6.2) could be
bypassed by a client that streams gigabytes — the file would be fully written to
disk before we ever counted a byte. This parser instead consumes the raw ASGI
body stream chunk by chunk, counting bytes as they arrive and aborting the moment
the cap is exceeded.

MVP scope: exactly one file part plus small text fields (``tenant_id``). A second
file part is rejected. Fields are capped at ``max_field_bytes``.

The parser only ever holds a bounded lookback window in memory (the tail that
could contain a split boundary marker); the file body streams straight to a
temporary file on disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import AsyncIterator

CRLF = b"\r\n"
_HEADER_CAP = 16 * 1024  # per-part header block
_PREAMBLE_CAP = 64 * 1024  # bytes before the first boundary we are willing to scan


class MultipartError(Exception):
    """Base class for multipart parse failures."""


class MalformedMultipart(MultipartError):
    """The body is not a well-formed multipart/form-data stream."""


class OversizedUpload(MultipartError):
    """The file part exceeded the hard cap — reject 413."""

    def __init__(self, size_so_far: int, limit: int) -> None:
        super().__init__(f"upload exceeds the {limit} byte limit")
        self.size_so_far = size_so_far
        self.limit = limit


@dataclass(frozen=True)
class ParsedPart:
    """One part of the multipart body.

    A file part carries ``temp_path`` (a NamedTemporaryFile, deleted on close)
    and ``filename``/``content_type``; a field part carries ``value``.
    """

    name: str
    filename: str | None = None
    content_type: str | None = None
    temp_path: Path | None = None
    value: bytes | None = None


def parse_boundary(content_type: str | None) -> str:
    """Extract the boundary token from a multipart Content-Type header."""
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        raise MalformedMultipart("expected multipart/form-data")
    match = re.search(r'boundary="?([^";]+)"?', content_type, flags=re.IGNORECASE)
    if not match:
        raise MalformedMultipart("multipart body is missing a boundary")
    boundary = match.group(1)
    if len(boundary) == 0 or len(boundary) > 200:
        raise MalformedMultipart("invalid boundary")
    return boundary


async def parse_parts(
    stream: AsyncIterator[bytes],
    boundary: str,
    *,
    max_file_bytes: int,
    max_field_bytes: int = 64 * 1024,
) -> list[ParsedPart]:
    """Consume the raw body stream and return the parsed parts.

    Raises MalformedMultipart (400) or OversizedUpload (413) on bad input.
    """
    delim = b"--" + boundary.encode()
    lookback = len(delim) + 4  # a split boundary can never span more than this
    chunks = _ChunkSource(stream)
    buf = bytearray()

    # --- preamble: find the first boundary line -----------------------------
    while True:
        idx = buf.find(delim)
        if idx >= 0:
            del buf[: idx + len(delim)]
            break
        if len(buf) > _PREAMBLE_CAP:
            raise MalformedMultipart("no multipart boundary found")
        chunk = await chunks.next()
        if chunk is None:
            raise MalformedMultipart("truncated multipart body (no boundary)")
        buf += chunk

    if await _await_delimiter_suffix(buf, chunks) == "final":
        raise MalformedMultipart("multipart body contains no parts")

    parts: list[ParsedPart] = []
    file_seen = False

    while True:
        # --- part headers ----------------------------------------------------
        head = await _read_until(buf, CRLF + CRLF, _HEADER_CAP, chunks)
        if head is None:
            raise MalformedMultipart("part headers unterminated")
        name, filename, ctype = _parse_headers(head)

        if filename is not None:  # ---- file part ----------------------------
            if file_seen:
                raise MalformedMultipart("only one file per upload is supported (MVP)")
            file_seen = True
            tmp = NamedTemporaryFile(prefix="veritas-upload-", delete=False)
            size = 0
            try:
                while True:
                    idx = buf.find(CRLF + delim)
                    if idx >= 0:
                        tmp.write(bytes(buf[:idx]))
                        size += idx
                        del buf[: idx + len(CRLF + delim)]
                        break
                    if len(buf) > lookback:
                        tmp.write(bytes(buf[:-lookback]))
                        size += len(buf) - lookback
                        del buf[:-lookback]
                    chunk = await chunks.next()
                    if chunk is None:
                        raise MalformedMultipart("file part unterminated (no closing boundary)")
                    if size + len(buf) + len(chunk) > max_file_bytes:
                        raise OversizedUpload(size + len(buf) + len(chunk), max_file_bytes)
                    buf += chunk
            except BaseException:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise
            tmp.close()
            parts.append(
                ParsedPart(name=name, filename=filename, content_type=ctype, temp_path=Path(tmp.name))
            )
        else:  # ---- field part ----------------------------------------------
            value = await _read_until(buf, CRLF + delim, max_field_bytes, chunks)
            if value is None:
                raise MalformedMultipart("field part unterminated")
            parts.append(ParsedPart(name=name, value=value))

        # --- after a boundary: final `--` or CRLF + next part ----------------
        if await _await_delimiter_suffix(buf, chunks) == "final":
            # any trailing epilogue is ignored per RFC 2046
            return parts


async def _await_delimiter_suffix(
    buf: bytearray, chunks: _ChunkSource
) -> str:
    """Decide what follows a boundary token, reading more chunks if needed.

    Returns ``"final"`` (the ``--boundary--`` closing delimiter, consumed) or
    ``"next"`` (the ``\\r\\n`` ending a regular boundary, consumed). A chunk can
    end exactly at the boundary token, so we must pull more data before deciding
    instead of assuming the suffix is already buffered.
    """
    while True:
        if buf.startswith(b"--"):
            del buf[:2]
            if buf.startswith(CRLF):
                del buf[:2]
            return "final"
        if buf.startswith(CRLF):
            del buf[:2]
            return "next"
        if len(buf) < 2:
            chunk = await chunks.next()
            if chunk is None:
                raise MalformedMultipart("truncated multipart body after boundary")
            buf += chunk
            continue
        raise MalformedMultipart("malformed boundary after part")


class _ChunkSource:
    """Pull wrapper around the ASGI request stream."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._it = stream.__aiter__()

    async def next(self) -> bytes | None:
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            return None


async def _read_until(
    buf: bytearray, marker: bytes, cap: int, chunks: _ChunkSource
) -> bytes | None:
    """Read from ``buf``+stream until ``marker``; return bytes before it (or None).

    Consumed content is removed from ``buf``; the marker itself stays in place
    for the caller to interpret. ``cap`` bounds how much we will buffer while
    searching (a hostile body cannot force unbounded buffering).
    """
    while True:
        idx = buf.find(marker)
        if idx >= 0:
            data = bytes(buf[:idx])
            del buf[: idx + len(marker)]
            return data
        if len(buf) > cap:
            raise MalformedMultipart("part too large")
        chunk = await chunks.next()
        if chunk is None:
            return None
        buf += chunk


def _parse_headers(raw: bytes) -> tuple[str, str | None, str | None]:
    """Return (field name, filename-or-None, content-type-or-None)."""
    name = filename = ctype = None
    for line in raw.decode("latin-1").split(CRLF.decode("latin-1")):
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "content-disposition":
            nm = re.search(r'name="([^"]*)"', value)
            fn = re.search(r'filename="([^"]*)"', value)
            name = nm.group(1) if nm else None
            filename = fn.group(1) if fn else None
        elif key == "content-type":
            ctype = value
    if not name:
        raise MalformedMultipart("part is missing a name")
    return name, filename, ctype
