from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .errors import WorkbenchError
from .util import sha256_bytes


@dataclass(slots=True)
class LiveryInfo:
    compressed_size: int
    uncompressed_size: int
    source_state: int
    car_id: int
    vlrc_offset: int
    info_offset: int
    gyvl_offset: int
    creator_tag: bytes
    wrapped_sha256: str
    payload_sha256: str

    def to_dict(self) -> dict:
        return {
            "compressed_size": self.compressed_size,
            "uncompressed_size": self.uncompressed_size,
            "source_state": self.source_state,
            "car_id": self.car_id,
            "vlrc_offset": self.vlrc_offset,
            "info_offset": self.info_offset,
            "gyvl_offset": self.gyvl_offset,
            "creator_tag_fingerprint": sha256_bytes(self.creator_tag)[:12],
            "wrapped_sha256": self.wrapped_sha256,
            "payload_sha256": self.payload_sha256,
        }


def unwrap_container(wrapped: bytes) -> bytes:
    if len(wrapped) < 8:
        raise WorkbenchError("C_livery 容器短于 8 字节")
    compressed_size, uncompressed_size = struct.unpack_from("<II", wrapped, 0)
    if compressed_size != len(wrapped) - 8:
        raise WorkbenchError("C_livery 压缩长度字段与文件大小不一致")
    try:
        payload = zlib.decompress(wrapped[8:])
    except zlib.error as exc:
        raise WorkbenchError(f"C_livery zlib 解压失败: {exc}") from exc
    if len(payload) != uncompressed_size:
        raise WorkbenchError("C_livery 解压长度字段与实际长度不一致")
    return payload


def wrap_container(payload: bytes) -> bytes:
    compressed = zlib.compress(payload)
    return struct.pack("<II", len(compressed), len(payload)) + compressed


def inspect_livery(wrapped: bytes) -> LiveryInfo:
    payload = unwrap_container(wrapped)
    vlrc = payload.find(b"vlrc")
    if vlrc < 0 or vlrc + 0x1A > len(payload):
        raise WorkbenchError("C_livery 缺少完整 vlrc 记录")
    source_state = struct.unpack_from("<I", payload, vlrc + 8)[0]
    car_id = struct.unpack_from("<I", payload, vlrc + 0x10)[0]
    gyvl = payload.find(b"gyvl", vlrc + 4)
    if gyvl < 0 or gyvl + 0x15 > len(payload):
        raise WorkbenchError("C_livery 缺少完整 gyvl 记录")
    info = payload.find(b"yrvl", vlrc + 4, gyvl)
    if info < 0 or info + 16 > gyvl:
        raise WorkbenchError("C_livery 缺少 gyvl 前的 yrvl 身份记录")
    creator_tag = payload[info + 8 : info + 16]
    compressed_size, uncompressed_size = struct.unpack_from("<II", wrapped, 0)
    return LiveryInfo(
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        source_state=source_state,
        car_id=car_id,
        vlrc_offset=vlrc,
        info_offset=info,
        gyvl_offset=gyvl,
        creator_tag=creator_tag,
        wrapped_sha256=sha256_bytes(wrapped),
        payload_sha256=sha256_bytes(payload),
    )


def make_private_livery(wrapped: bytes, *, creator_tag: bytes, expected_car_id: int) -> bytes:
    if len(creator_tag) != 8:
        raise WorkbenchError("身份种子的 creator tag 必须恰好为 8 字节")
    before = inspect_livery(wrapped)
    if before.car_id != expected_car_id:
        raise WorkbenchError(
            f"C_livery 车型 ID {before.car_id} 与 header {expected_car_id} 不一致"
        )
    payload = bytearray(unwrap_container(wrapped))
    struct.pack_into("<I", payload, before.vlrc_offset + 8, 0)
    payload[before.info_offset + 8 : before.info_offset + 16] = creator_tag
    result = wrap_container(bytes(payload))
    after = inspect_livery(result)
    if after.source_state != 0 or after.car_id != expected_car_id:
        raise WorkbenchError("私人 C_livery 回读验证失败")
    if after.creator_tag != creator_tag:
        raise WorkbenchError("私人 C_livery 身份写入验证失败")
    return result
