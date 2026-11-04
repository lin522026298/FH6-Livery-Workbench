from __future__ import annotations
import shutil
from pathlib import Path
from .container_codec import inspect_livery, make_private_livery
from .errors import WorkbenchError
from .header_codec import build_header, parse_header, private_header_from_seed
from .save_scan import SaveItem
from .util import json_dump_atomic, safe_slug, sha256_file, timestamp14, utc_now_iso

def stage_export(
    export_folder: Path,
    seed: SaveItem,
    workspace: Path,
    *,
    final_name: str | None = None,
) -> Path:
    """Sanitize an FLS export again and stage it for an additive install."""
    if seed.header is None or seed.header.published:
        raise WorkbenchError("必须选择自己的未发布完整涂装作为身份种子")
    header_path = export_folder / "header"
    payload_path = export_folder / "C_livery"
    if not header_path.is_file() or not payload_path.is_file():
        raise WorkbenchError("FLS 导出目录必须包含 header 和 C_livery")
    export_header = parse_header(header_path.read_bytes())
    export_info = inspect_livery(payload_path.read_bytes())
    if export_header.car_id != export_info.car_id:
        raise WorkbenchError("FLS 导出 header 与 C_livery 的车型 ID 不一致")
    if export_header.car_id <= 0:
        raise WorkbenchError("FLS 导出车型 ID 无效")

    name = safe_slug(final_name or export_header.name or "Private Livery", limit=96)
    identity = private_header_from_seed(export_header, seed.header, name=name)
    private_payload = make_private_livery(
        payload_path.read_bytes(),
        creator_tag=identity.creator_tag,
        expected_car_id=identity.car_id,
    )
    base = f"Livery_{identity.car_id}_{timestamp14()}"
    ready = workspace / "ready" / base
    if ready.exists():
        raise WorkbenchError(f"待安装目录已存在：{ready}")
    ready.mkdir(parents=True)
    (ready / "header").write_bytes(build_header(identity))
    (ready / "C_livery").write_bytes(private_payload)
    for candidate in ("bigThumb.webp", "bigThumb.png"):
        source = export_folder / candidate
        if source.is_file():
            shutil.copy2(source, ready / candidate)
            break

    files = {file.name: sha256_file(file) for file in ready.iterdir() if file.is_file()}
    json_dump_atomic(
        ready / "workbench-ready.json",
        {
            "schema": 1,
            "kind": "fh6-private-livery-ready",
            "created_at": utc_now_iso(),
            "base": base,
            "platform": seed.save_root.platform,
            "save_root": str(seed.save_root.path),
            "identity_seed_base": seed.base,
            "header": identity.to_public_dict(),
            "livery": inspect_livery(private_payload).to_dict(),
            "files": files,
        },
    )
    return ready
