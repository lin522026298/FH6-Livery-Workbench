from __future__ import annotations

import copy
import gzip
import json
import uuid
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .util import json_dump_atomic, sha256_file, utc_now_iso

SECTION_NAMES = [
    "Front",
    "Back",
    "Top",
    "Left",
    "Right",
    "Spoiler",
    "FrontWindshield",
    "BackWindshield",
    "TopWindow",
    "LeftWindow",
    "RightWindow",
]


def load_project(path: Path) -> dict[str, Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            project = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"无法读取 .3so：{exc}") from exc
    if project.get("format") not in ("fls_editor_project", "fh6_editor_project"):
        raise WorkbenchError("文件不是受支持的 FLS .3so 工程")
    if int(project.get("version", 0)) not in (1, 2, 3, 4):
        raise WorkbenchError(".3so 工程版本高于 Workbench 支持范围")
    if not isinstance(project.get("root", {}).get("children"), list):
        raise WorkbenchError(".3so 缺少场景树")
    return project


def save_project(path: Path, project: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9, newline="\n") as stream:
        json.dump(project, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def _identity_transform() -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "scale_x": 1.0, "scale_y": 1.0, "rotation": 0.0, "skew": 0.0}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _detach_node(node: dict[str, Any]) -> int:
    kind = node.get("kind", "shape")
    node["id"] = _new_id("group" if kind == "group" else "layer")
    node["locked"] = False
    if kind == "shape":
        debug = node.setdefault("debug", {})
        debug.update(
            {
                "source_shape": 0,
                "abs_offset": 0,
                "marker": "",
                "flags": 0,
                "source_logo_id": 0,
                "has_source_transform": False,
            }
        )
        debug.pop("source_transform", None)
        return 1
    if kind == "guide":
        raise WorkbenchError("贴纸工程含 Guide 图层；请先在 FLS 中把它转换为实际形状")
    debug = node.setdefault("debug", {})
    debug.update(
        {
            "source_abs_pos": 0,
            "pending_transform_marker": "",
            "inline_transform_marker": "",
            "effective_transform_marker": "",
            "header_control_bytes": "",
            "flags": 0,
            "source_parent_id": "",
            "source_previous_sibling_id": "",
            "source_previous_group_depth": 0,
            "source_child_ids": [],
        }
    )
    node["is_livery_section"] = False
    node["livery_section_slot"] = -1
    return sum(_detach_node(child) for child in node.get("children", []))


def merge_group_project(
    livery_path: Path,
    group_path: Path,
    section_slot: int,
    output_path: Path,
    *,
    group_name: str | None = None,
) -> dict[str, Any]:
    if section_slot < 0 or section_slot >= len(SECTION_NAMES):
        raise WorkbenchError("分面编号必须在 0 到 10 之间")
    livery = load_project(livery_path)
    sticker = load_project(group_path)
    if not livery.get("is_livery"):
        raise WorkbenchError("目标 .3so 不是完整涂装工程")
    if sticker.get("is_livery"):
        raise WorkbenchError("贴纸 .3so 必须是 Group 工程，而不是完整涂装")

    section = next(
        (
            node
            for node in livery["root"]["children"]
            if node.get("kind") == "group"
            and node.get("is_livery_section")
            and int(node.get("livery_section_slot", -1)) == section_slot
        ),
        None,
    )
    if section is None:
        raise WorkbenchError(f"完整涂装工程缺少 {SECTION_NAMES[section_slot]} 分面")
    children = copy.deepcopy(sticker["root"]["children"])
    if not children:
        raise WorkbenchError("贴纸工程没有可合并图层")
    shape_count = sum(_detach_node(node) for node in children)
    wrapper = {
        "kind": "group",
        "id": _new_id("sticker"),
        "name": group_name or sticker.get("name") or "Imported Sticker",
        "transform": _identity_transform(),
        "opacity": 1.0,
        "visible": True,
        "locked": False,
        "is_livery_section": False,
        "livery_section_slot": -1,
        "debug": {
            "source_abs_pos": 0,
            "pending_transform_marker": "",
            "inline_transform_marker": "",
            "effective_transform_marker": "",
            "header_control_bytes": "",
            "flags": 0,
            "source_parent_id": "",
            "source_previous_sibling_id": "",
            "source_previous_group_depth": 0,
            "source_child_ids": [],
        },
        "children": children,
    }
    section.setdefault("children", []).append(wrapper)
    save_project(output_path, livery)
    report = {
        "schema": 1,
        "created_at": utc_now_iso(),
        "livery_project": str(livery_path),
        "group_project": str(group_path),
        "output_project": str(output_path),
        "section_slot": section_slot,
        "section_name": SECTION_NAMES[section_slot],
        "shape_count": shape_count,
        "output_sha256": sha256_file(output_path),
    }
    json_dump_atomic(output_path.with_suffix(output_path.suffix + ".merge.json"), report)
    return report


def painter_shapes_to_project(source: Path, output: Path) -> dict[str, Any]:
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"无法读取 painter-shapes-v1：{exc}") from exc
    if data.get("schema") != "painter-shapes-v1":
        raise WorkbenchError("输入 JSON 的 schema 不是 painter-shapes-v1")
    shapes = data.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        raise WorkbenchError("painter-shapes-v1 没有 shapes")
    children = []
    for index, shape in enumerate(shapes, 1):
        shape_id = int(shape.get("shape_id", 0))
        if not 0 <= shape_id <= 0xFFFF:
            raise WorkbenchError(f"第 {index} 个 shape_id 超出 u16 范围")
        rgba = str(shape.get("color", "#FFFFFFFF")).lstrip("#")
        if len(rgba) == 6:
            rgba += "FF"
        try:
            red, green, blue, alpha = bytes.fromhex(rgba)
        except ValueError as exc:
            raise WorkbenchError(f"第 {index} 个颜色不是 #RRGGBBAA") from exc
        children.append(
            {
                "kind": "shape",
                "id": _new_id("shape"),
                "name": str(shape.get("name") or f"Shape {index}"),
                "transform": {
                    "x": float(shape.get("x", 0.0)),
                    "y": float(shape.get("y", 0.0)),
                    "scale_x": float(shape.get("scale_x", 1.0)),
                    "scale_y": float(shape.get("scale_y", 1.0)),
                    "rotation": float(shape.get("rotation", 0.0)) % 360.0,
                    "skew": float(shape.get("skew", 0.0)),
                },
                "opacity": 1.0,
                "visible": bool(shape.get("visible", True)),
                "locked": False,
                "mask": bool(shape.get("mask", False)),
                "color": [blue, green, red, alpha],
                "visual": {"kind": "vector", "shape_id": shape_id},
                "debug": {
                    "source_shape": 0,
                    "abs_offset": 0,
                    "marker": "",
                    "flags": 0,
                    "source_logo_id": 0,
                    "has_source_transform": False,
                },
            }
        )
    project = {
        "format": "fls_editor_project",
        "version": 3,
        "name": str(data.get("name") or source.stem),
        "source_folder": "",
        "is_livery": False,
        "horizontal_guidelines": [],
        "vertical_guidelines": [],
        "source_dec_prefix": "",
        "source_header": "",
        "root": {"children": children},
        "color_swatches": [],
    }
    save_project(output, project)
    return {"shapes": len(children), "output": str(output), "sha256": sha256_file(output)}
