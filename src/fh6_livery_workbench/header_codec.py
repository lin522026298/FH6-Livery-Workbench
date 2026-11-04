from __future__ import annotations

import base64
import struct
import uuid
from dataclasses import dataclass
from datetime import date

from .errors import WorkbenchError

CURRENT_FORMAT_VERSION = 7
CREATOR_TAG_SIZE = 8
FIELD_BLOCK_SIZE = 16
GUID_SIZE = 16
SECTION_PREFIX_SIZE = 28
KNOWN_SECTION_MARKERS = frozenset({b"\x01\x00", b"\x01\x01", b"\x01\x02"})
DEFAULT_PRIVATE_SECTION_MARKER = b"\x01\x02"


class HeaderTruncatedError(WorkbenchError):
    """The header ends before a required field can be read."""


class UnsupportedHeaderVariantError(WorkbenchError):
    """The header is aligned, but its section marker is not understood."""


def _need(data: bytes, offset: int, count: int, label: str) -> None:
    if offset < 0 or count < 0 or offset + count > len(data):
        raise HeaderTruncatedError(f"header 在 {label} 处截断")


def _u16(data: bytes, offset: int) -> int:
    _need(data, offset, 2, "u16")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    _need(data, offset, 4, "u32")
    return struct.unpack_from("<I", data, offset)[0]


def _read_utf16(data: bytes, offset: int, count: int, label: str) -> tuple[str, int]:
    if count > 4096:
        raise WorkbenchError(f"header {label} 长度异常: {count}")
    size = count * 2
    _need(data, offset, size, label)
    return data[offset : offset + size].decode("utf-16-le", errors="replace"), offset + size


@dataclass(slots=True)
class HeaderMetadata:
    format_version: int = CURRENT_FORMAT_VERSION
    name: str = ""
    published: bool = False
    description: str = ""
    year: int = 0
    month: int = 0
    day: int = 0
    field_block: bytes = b""
    creator_tag: bytes = b""
    creator_name: str = ""
    section_prefix: bytes = b""
    # The exact semantics of the second byte are not yet established.  Keep the
    # observed two-byte marker and its seven opaque bytes losslessly instead of
    # inventing a publication/privacy meaning for them.
    section_marker: bytes = DEFAULT_PRIVATE_SECTION_MARKER
    section_marker_tail: bytes = b""
    type_value: int = 0
    car_id: int = 0
    guid: bytes = b""
    trailing: bytes = b""

    @property
    def identity_fingerprint(self) -> str:
        """Non-secret short identity correlation value for diagnostics."""
        import hashlib

        return hashlib.sha256(self.creator_tag).hexdigest()[:12]

    def to_public_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "name": self.name,
            "published": self.published,
            "creator_name": self.creator_name,
            "creator_tag_fingerprint": self.identity_fingerprint,
            "section_marker_hex": self.section_marker.hex(" "),
            "type_value": self.type_value,
            "car_id": self.car_id,
            "guid": str(uuid.UUID(bytes=self.guid)) if len(self.guid) == GUID_SIZE else "",
        }

    def to_fls_json(self) -> dict:
        b64 = lambda value: base64.b64encode(value).decode("ascii")
        return {
            "format_version": self.format_version,
            "name": self.name,
            "published": False,
            "description": "",
            "year": self.year,
            "month": self.month,
            "day": self.day,
            "field_block": b64(self.field_block),
            "creator_tag": b64(self.creator_tag),
            "creator_name": self.creator_name,
            "section_prefix": b64(self.section_prefix),
            "type_value": self.type_value,
            "car_id": self.car_id,
            "guid": b64(self.guid),
            "trailing": b64(self.trailing),
            "published_tail": "",
        }


def parse_header(data: bytes) -> HeaderMetadata:
    if len(data) < 12:
        raise HeaderTruncatedError("header 太短")
    offset = 0
    version = _u32(data, offset)
    offset += 4
    name_len = _u32(data, offset)
    offset += 4
    name, offset = _read_utf16(data, offset, name_len, "name")

    desc_len = _u32(data, offset)
    offset += 4
    description = ""
    if desc_len:
        description, offset = _read_utf16(data, offset, desc_len, "description")

    _need(data, offset, 4 + FIELD_BLOCK_SIZE + CREATOR_TAG_SIZE + 4, "metadata")
    year = _u16(data, offset)
    month = data[offset + 2]
    day = data[offset + 3]
    offset += 4
    field_block = data[offset : offset + FIELD_BLOCK_SIZE]
    offset += FIELD_BLOCK_SIZE
    creator_tag = data[offset : offset + CREATOR_TAG_SIZE]
    offset += CREATOR_TAG_SIZE
    creator_len = _u32(data, offset)
    offset += 4
    creator_name, offset = _read_utf16(data, offset, creator_len, "creator")

    _need(data, offset, SECTION_PREFIX_SIZE + 9 + 8 + GUID_SIZE, "section 3")
    section_prefix = data[offset : offset + SECTION_PREFIX_SIZE]
    offset += SECTION_PREFIX_SIZE
    section_marker = data[offset : offset + 2]
    if section_marker not in KNOWN_SECTION_MARKERS:
        raise UnsupportedHeaderVariantError(
            "header section 标记 "
            f"{section_marker.hex(' ') or '<empty>'} 尚未支持；"
            "仅支持已观察到的 01 00、01 01、01 02"
        )
    section_marker_tail = data[offset + 2 : offset + 9]
    offset += 9
    type_value = _u32(data, offset)
    car_id = _u32(data, offset + 4)
    offset += 8
    guid = data[offset : offset + GUID_SIZE]
    offset += GUID_SIZE

    return HeaderMetadata(
        format_version=version,
        name=name,
        published=bool(desc_len),
        description=description,
        year=year,
        month=month,
        day=day,
        field_block=field_block,
        creator_tag=creator_tag,
        creator_name=creator_name,
        section_prefix=section_prefix,
        section_marker=section_marker,
        section_marker_tail=section_marker_tail,
        type_value=type_value,
        car_id=car_id,
        guid=guid,
        trailing=data[offset:],
    )


def build_header(meta: HeaderMetadata) -> bytes:
    if meta.published:
        raise WorkbenchError("Workbench 只允许构建未发布的私人 header")
    name = meta.name.encode("utf-16-le")
    creator = meta.creator_name.encode("utf-16-le")
    field_block = (meta.field_block or bytes(FIELD_BLOCK_SIZE))[:FIELD_BLOCK_SIZE].ljust(
        FIELD_BLOCK_SIZE, b"\0"
    )
    creator_tag = (meta.creator_tag or bytes(CREATOR_TAG_SIZE))[:CREATOR_TAG_SIZE].ljust(
        CREATOR_TAG_SIZE, b"\0"
    )
    section_prefix = (meta.section_prefix or bytes(SECTION_PREFIX_SIZE))[
        :SECTION_PREFIX_SIZE
    ].ljust(SECTION_PREFIX_SIZE, b"\0")
    section_marker = meta.section_marker or DEFAULT_PRIVATE_SECTION_MARKER
    if section_marker not in KNOWN_SECTION_MARKERS:
        raise UnsupportedHeaderVariantError(
            f"不允许构建未知 header section 标记 {section_marker.hex(' ')}"
        )
    section_marker_tail = (meta.section_marker_tail or bytes(7))[:7].ljust(7, b"\0")
    guid = meta.guid if len(meta.guid) == GUID_SIZE else uuid.uuid4().bytes
    return b"".join(
        [
            struct.pack("<II", meta.format_version, len(name) // 2),
            name,
            struct.pack("<I", 0),
            struct.pack("<HBB", meta.year, meta.month, meta.day),
            field_block,
            creator_tag,
            struct.pack("<I", len(creator) // 2),
            creator,
            section_prefix,
            section_marker + section_marker_tail,
            struct.pack("<II", meta.type_value, meta.car_id),
            guid,
            meta.trailing,
        ]
    )


def private_header_from_seed(
    target: HeaderMetadata,
    seed: HeaderMetadata,
    *,
    name: str,
    renew_guid: bool = True,
) -> HeaderMetadata:
    if seed.published:
        raise WorkbenchError("身份种子必须是自己的未发布完整涂装")
    today = date.today()
    field_block = bytearray(FIELD_BLOCK_SIZE)
    field_block[12] = 2
    return HeaderMetadata(
        format_version=CURRENT_FORMAT_VERSION,
        name=name,
        published=False,
        description="",
        year=today.year,
        month=today.month,
        day=0,
        field_block=bytes(field_block),
        creator_tag=seed.creator_tag,
        creator_name=seed.creator_name,
        section_prefix=bytes(SECTION_PREFIX_SIZE),
        # Generate the private header from the user's unpublished identity seed.
        # Never propagate this opaque field from the downloaded target.
        section_marker=(
            seed.section_marker
            if seed.section_marker in KNOWN_SECTION_MARKERS
            else DEFAULT_PRIVATE_SECTION_MARKER
        ),
        section_marker_tail=bytes(7),
        type_value=target.type_value,
        car_id=target.car_id,
        guid=uuid.uuid4().bytes if renew_guid else target.guid,
        trailing=b"",
    )
