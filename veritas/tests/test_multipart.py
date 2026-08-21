"""Unit tests for the streaming multipart parser (app/uploads/multipart.py).

Feeds synthetic multipart bodies through the parser in awkward chunk sizes
(1 byte, 7 bytes, all-at-once) to shake out split-boundary bugs, and verifies
the hard size cap is enforced while streaming (never after buffering).
"""
from __future__ import annotations

import asyncio

import pytest

from app.uploads.multipart import (
    MalformedMultipart,
    OversizedUpload,
    ParsedPart,
    parse_boundary,
    parse_parts,
)

BOUNDARY = "XbndY01z"
CSV = b"date,amount\n2026-01-01,42\n2026-01-02,7\n"
TENANT = "7f9d1b3e-0000-4000-8000-000000000001"


def _multipart_body(*, tenant: str = TENANT, file_name: str = "ledger.csv",
                    file_ct: str = "text/csv", file_bytes: bytes = CSV) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="tenant_id"\r\n\r\n'
        f"{tenant}\r\n"
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        f"Content-Type: {file_ct}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{BOUNDARY}--\r\n".encode()


def _parse(body: bytes, chunk_size: int = 64, max_file_bytes: int = 10 * 1024 * 1024, **kwargs):
    async def run():
        async def gen():
            for i in range(0, len(body), chunk_size):
                yield body[i : i + chunk_size]

        return await parse_parts(gen(), BOUNDARY, max_file_bytes=max_file_bytes, **kwargs)

    return asyncio.run(run())


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
def test_parses_field_and_file_part(chunk_size):
    parts = _parse(_multipart_body(), chunk_size=chunk_size)
    by_name = {p.name: p for p in parts}
    assert by_name["tenant_id"].value == TENANT.encode()
    f = by_name["file"]
    assert f.filename == "ledger.csv"
    assert f.content_type == "text/csv"
    assert f.temp_path is not None
    assert f.temp_path.read_bytes() == CSV
    assert f.temp_path.stat().st_size == len(CSV)


def test_field_after_file_part_order_agnostic():
    """Parts may arrive in any order; the parser must not assume field-first."""
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="x.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + b"a,b\n" + f"\r\n--{BOUNDARY}\r\n".encode() + (
        f'Content-Disposition: form-data; name="tenant_id"\r\n\r\n'
        f"{TENANT}\r\n--{BOUNDARY}--\r\n"
    ).encode()
    parts = _parse(body, chunk_size=1)
    by_name = {p.name: p for p in parts}
    assert by_name["tenant_id"].value == TENANT.encode()
    assert by_name["file"].temp_path.read_bytes() == b"a,b\n"


def test_size_cap_enforced_on_the_stream():
    """The cap must trip mid-stream — not after the whole body is buffered."""
    body = _multipart_body(file_bytes=b"x" * 100)
    with pytest.raises(OversizedUpload):
        _parse(body, chunk_size=7, max_file_bytes=50)


def test_truncated_body_rejected():
    body = _multipart_body()[:-20]  # cut the final boundary
    with pytest.raises(MalformedMultipart):
        _parse(body, chunk_size=13)


def test_malformed_closing_delimiter_rejected():
    """A closing boundary followed by junk (not `--` or CRLF) is malformed."""
    body = _multipart_body().replace(
        f"\r\n--{BOUNDARY}--\r\n".encode(), f"\r\n--{BOUNDARY}XX\r\n".encode()
    )
    with pytest.raises(MalformedMultipart):
        _parse(body)


def test_epilogue_after_final_boundary_ignored():
    """RFC 2046: content after the final boundary is epilogue and must be ignored."""
    body = _multipart_body() + b"trailing-garbage-is-legal-epilogue"
    parts = _parse(body, chunk_size=3)
    assert any(p.filename == "ledger.csv" for p in parts)


def test_second_file_part_rejected():
    body = _multipart_body()[:-len(f"\r\n--{BOUNDARY}--\r\n".encode())] + (
        f"\r\n--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="second.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\nb,c\n\r\n--{BOUNDARY}--\r\n"
    ).encode()
    with pytest.raises(MalformedMultipart):
        _parse(body)


def test_parse_boundary_extraction():
    assert parse_boundary(f"multipart/form-data; boundary={BOUNDARY}") == BOUNDARY
    assert parse_boundary(f'multipart/form-data; boundary="{BOUNDARY}"') == BOUNDARY
    with pytest.raises(MalformedMultipart):
        parse_boundary("multipart/form-data")
    with pytest.raises(MalformedMultipart):
        parse_boundary("application/json")


def test_oversized_error_carries_limits():
    err = OversizedUpload(size_so_far=60, limit=50)
    assert err.limit == 50
    assert "50" in str(err)


def test_empty_file_part_ok():
    body = _multipart_body(file_bytes=b"")
    parts = _parse(body)
    f = next(p for p in parts if p.temp_path is not None)
    assert f.temp_path.read_bytes() == b""
