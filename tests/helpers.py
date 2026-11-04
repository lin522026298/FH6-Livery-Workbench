from __future__ import annotations

import struct
import uuid
from datetime import date
from pathlib import Path

from fh6_livery_workbench.container_codec import wrap_container
from fh6_livery_workbench.header_codec import (
    CREATOR_TAG_SIZE,
    FIELD_BLOCK_SIZE,
    SECTION_PREFIX_SIZE,
    HeaderMetadata,
    build_header,
)


def draft_header(
    name: str,
    car_id: int,
    creator: str,
    tag: bytes,
    *,
    section_marker: bytes = b"\x01\x02",
) -> bytes:
    field = bytearray(FIELD_BLOCK_SIZE)
    field[12] = 2
    today = date.today()
    return build_header(
        HeaderMetadata(
            name=name,
            year=today.year,
            month=today.month,
            field_block=bytes(field),
            creator_tag=tag,
            creator_name=creator,
            section_prefix=bytes(SECTION_PREFIX_SIZE),
            section_marker=section_marker,
            type_value=7,
            car_id=car_id,
            guid=uuid.uuid4().bytes,
        )
    )


def livery(car_id: int, tag: bytes, source_state: int = 1) -> bytes:
    assert len(tag) == CREATOR_TAG_SIZE
    gyvl = b"gyvl" + bytes(0x11) + bytes(32)
    payload = bytearray(b"vlrc")
    payload += struct.pack("<IIIIIH", 2, source_state, 0, car_id, 0, 0)
    payload += b"yrvl" + struct.pack("<I", 19) + tag + struct.pack("<I", 1)
    payload += b"\0" + struct.pack("<I", len(gyvl)) + gyvl
    payload += b"yrvl" + bytes(48)
    payload += b"yrvl" + bytes(12)
    return wrap_container(bytes(payload))


def write_pgs_item(root: Path, base: str, *, car_id: int, creator: str, tag: bytes, state: int) -> Path:
    item = root / base
    item.mkdir(parents=True)
    (item / "header").write_bytes(draft_header(base, car_id, creator, tag))
    (item / "C_livery").write_bytes(livery(car_id, tag, state))
    return item
