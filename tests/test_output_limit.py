"""Contract tests for output limiting (arthas_mcp_proxy.output_limit).

``limit_output`` bounds a diagnostic text payload to ``max_chars`` characters
and reports exactly how much was kept/dropped (``truncated``,
``original_chars``, ``returned_chars``).  ``paginate_output`` walks the same
payload in fixed-size pages: the first page (``cursor=None``) returns the
leading slice plus an opaque ``next_cursor``; handing that cursor back yields
the next slice, an invalid cursor raises ``DomainError`` with
``OUTPUT_CURSOR_INVALID``, and the final page returns ``next_cursor=None``.
B4: output-limit contract only - no server integration yet.
"""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.output_limit import limit_output, paginate_output


@pytest.mark.contract
def test_long_text_is_truncated_to_max_chars() -> None:
    text = "x" * 100
    out = limit_output(text, max_chars=5)
    assert out.text == "xxxxx"
    assert len(out.text) <= 5
    assert out.truncated is True
    assert out.original_chars == 100
    assert out.returned_chars == 5


@pytest.mark.contract
def test_short_text_is_not_truncated() -> None:
    text = "ok"
    out = limit_output(text, max_chars=5)
    assert out.text == "ok"
    assert out.truncated is False
    assert out.original_chars == 2
    assert out.returned_chars == 2


@pytest.mark.contract
def test_exact_max_chars_boundary_is_not_truncated() -> None:
    text = "abcde"
    out = limit_output(text, max_chars=5)
    assert out.text == "abcde"
    assert out.truncated is False
    assert out.original_chars == 5
    assert out.returned_chars == 5


@pytest.mark.contract
def test_empty_text_is_not_truncated() -> None:
    out = limit_output("", max_chars=5)
    assert out.text == ""
    assert out.truncated is False
    assert out.original_chars == 0
    assert out.returned_chars == 0


@pytest.mark.contract
def test_negative_max_chars_raises_value_error() -> None:
    with pytest.raises(ValueError):
        limit_output("abc", max_chars=-1)


# ── Pagination (paginate_output) ─────────────────────────────────────────────


@pytest.mark.contract
def test_paginate_first_page_returns_slice_and_next_cursor() -> None:
    text = "x" * 100
    page = paginate_output(text, cursor=None, max_chars=5)
    assert page.text == "x" * 5
    assert page.next_cursor is not None


@pytest.mark.contract
def test_paginate_valid_next_cursor_returns_next_slice() -> None:
    text = "x" * 100
    first = paginate_output(text, cursor=None, max_chars=5)
    second = paginate_output(text, cursor=first.next_cursor, max_chars=5)
    assert second.text == "x" * 5
    assert second.next_cursor is not None


@pytest.mark.contract
def test_paginate_invalid_cursor_raises_domain_error() -> None:
    with pytest.raises(DomainError) as excinfo:
        paginate_output("x" * 10, cursor="bogus", max_chars=5)
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID


@pytest.mark.contract
def test_paginate_negative_cursor_raises_domain_error() -> None:
    with pytest.raises(DomainError) as excinfo:
        paginate_output("x" * 10, cursor="-3", max_chars=5)
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID


@pytest.mark.contract
def test_paginate_end_returns_empty_page_with_no_next_cursor() -> None:
    page = paginate_output("abcdef", cursor="6", max_chars=5)
    assert page.text == ""
    assert page.next_cursor is None


@pytest.mark.contract
def test_paginate_short_text_ends_on_first_page() -> None:
    page = paginate_output("ok", cursor=None, max_chars=5)
    assert page.text == "ok"
    assert page.next_cursor is None


@pytest.mark.contract
def test_paginate_walks_full_text_without_loss() -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(100))
    cursor = None
    chunks = []
    while True:
        page = paginate_output(text, cursor=cursor, max_chars=7)
        chunks.append(page.text)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert "".join(chunks) == text


@pytest.mark.contract
def test_secure_cursor_is_opaque_and_bound_to_job() -> None:
    first = paginate_output("abcdefghij", cursor=None, max_chars=3, job_id="job-a")
    assert first.next_cursor is not None
    assert not first.next_cursor.isdigit()
    assert paginate_output("abcdefghij", first.next_cursor, 3, job_id="job-a").text == "def"
    with pytest.raises(DomainError) as excinfo:
        paginate_output("abcdefghij", first.next_cursor, 3, job_id="job-b")
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID


@pytest.mark.contract
def test_secure_cursor_tampering_is_rejected() -> None:
    first = paginate_output("abcdefghij", cursor=None, max_chars=3, job_id="job-a")
    assert first.next_cursor is not None
    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    with pytest.raises(DomainError) as excinfo:
        paginate_output("abcdefghij", tampered, 3, job_id="job-a")
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID


@pytest.mark.contract
def test_secure_cursor_expiry_is_rejected() -> None:
    first = paginate_output(
        "abcdefghij", cursor=None, max_chars=3, job_id="job-a", cursor_ttl_seconds=0
    )
    assert first.next_cursor is not None
    with pytest.raises(DomainError) as excinfo:
        paginate_output("abcdefghij", first.next_cursor, 3, job_id="job-a")
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID


@pytest.mark.contract
def test_secure_endpoint_rejects_legacy_numeric_cursor() -> None:
    with pytest.raises(DomainError) as excinfo:
        paginate_output("abcdefghij", "3", 3, job_id="job-a")
    assert excinfo.value.code is ErrorCode.OUTPUT_CURSOR_INVALID
