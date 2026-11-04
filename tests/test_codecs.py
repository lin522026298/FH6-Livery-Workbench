from __future__ import annotations

import uuid

import pytest

from fh6_livery_workbench.container_codec import inspect_livery, make_private_livery
from fh6_livery_workbench.header_codec import (
    HeaderMetadata,
    HeaderTruncatedError,
    UnsupportedHeaderVariantError,
    build_header,
    parse_header,
    private_header_from_seed,
)

from helpers import draft_header, livery


def test_header_roundtrip_and_private_identity() -> None:
    target = parse_header(
        draft_header("Target", 1234, "Foreign", b"FOREIGN!", section_marker=b"\x01\x01")
    )
    seed = parse_header(
        draft_header("Seed", 999, "Local User", b"LOCAL123", section_marker=b"\x01\x00")
    )
    private = private_header_from_seed(target, seed, name="Private Target")
    decoded = parse_header(build_header(private))
    assert decoded.name == "Private Target"
    assert decoded.car_id == 1234
    assert decoded.creator_name == "Local User"
    assert decoded.creator_tag == b"LOCAL123"
    assert decoded.published is False
    assert decoded.guid != target.guid
    assert decoded.section_marker == b"\x01\x00"


@pytest.mark.parametrize("marker", [b"\x01\x00", b"\x01\x01", b"\x01\x02"])
def test_known_header_section_variants_roundtrip(marker: bytes) -> None:
    decoded = parse_header(draft_header("Variant", 3726, "Creator", b"LOCAL123", section_marker=marker))
    assert decoded.section_marker == marker
    assert decoded.car_id == 3726
    assert parse_header(build_header(decoded)).section_marker == marker


def test_unknown_header_variant_is_explicitly_rejected() -> None:
    value = bytearray(draft_header("Unknown", 3726, "Creator", b"LOCAL123"))
    position = value.rfind(b"\x01\x02" + bytes(7))
    assert position > 0
    value[position : position + 2] = b"\x01\x03"
    with pytest.raises(UnsupportedHeaderVariantError, match="01 03"):
        parse_header(bytes(value))


def test_truncated_header_is_distinguished() -> None:
    with pytest.raises(HeaderTruncatedError, match="截断|太短"):
        parse_header(draft_header("Cut", 1, "Creator", b"LOCAL123")[:-4])


def test_livery_private_reauthor_changes_only_identity_state() -> None:
    wrapped = livery(1234, b"FOREIGN!", source_state=1)
    before = inspect_livery(wrapped)
    output = make_private_livery(wrapped, creator_tag=b"LOCAL123", expected_car_id=1234)
    after = inspect_livery(output)
    assert before.source_state == 1
    assert after.source_state == 0
    assert after.car_id == before.car_id == 1234
    assert after.creator_tag == b"LOCAL123"
    assert after.payload_sha256 != before.payload_sha256
