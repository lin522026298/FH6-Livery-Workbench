from __future__ import annotations

import ctypes
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .container_codec import LiveryInfo, inspect_livery
from .header_codec import (
    HeaderMetadata,
    HeaderTruncatedError,
    UnsupportedHeaderVariantError,
    parse_header,
)
from .util import resource_root

FH6_STEAM_APP_ID = 2483190
ITEM_RE = re.compile(r"^([A-Za-z]+)_(\d{3,6})(?:_m\d+)?_(\d{14})(?:\.(.+))?$")


@dataclass(slots=True)
class SaveRoot:
    platform: str
    account_label: str
    path: Path

    @property
    def display_name(self) -> str:
        return f"{self.platform.upper()} · {self.path.name}"


@dataclass(slots=True)
class SaveItem:
    save_root: SaveRoot
    base: str
    item_type: str
    car_id: int
    timestamp: datetime | None
    is_directory: bool
    files: dict[str, Path] = field(default_factory=dict)
    header: HeaderMetadata | None = None
    header_error: str = ""
    header_status: str = ""
    livery: LiveryInfo | None = None
    livery_error: str = ""
    car_id_error: str = ""

    @property
    def name(self) -> str:
        return self.header.name if self.header else self.base

    @property
    def creator(self) -> str:
        return self.header.creator_name if self.header else ""

    @property
    def published(self) -> bool | None:
        return self.header.published if self.header is not None else None

    @property
    def state_label(self) -> str:
        if self.published is None:
            return "未知"
        return "已分享" if self.published else "私人"

    @property
    def read_warnings(self) -> list[str]:
        return [value for value in (self.header_error, self.livery_error, self.car_id_error) if value]

    @property
    def complete_livery(self) -> bool:
        return self.item_type == "Livery" and "C_livery" in self.files and "header" in self.files

    @property
    def stable_id(self) -> str:
        return f"{self.save_root.platform}:{self.save_root.path}:{self.base}"

    @property
    def thumbnail(self) -> Path | None:
        for key in (
            "bigThumb.webp",
            "bigThumb.png",
            "smallThumb.webp",
            "smallThumb.png",
            "Thumb.webp",
            "Thumb.png",
            "thumb",
        ):
            if key in self.files:
                return self.files[key]
        return None


def _logical_drives() -> list[Path]:
    if os.name != "nt":
        return [Path("/")]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(f"{chr(65 + index)}:\\") for index in range(26) if mask & (1 << index)]


def _steam_root() -> Path | None:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                value = Path(winreg.QueryValueEx(key, "SteamPath")[0])
                if (value / "userdata").is_dir():
                    return value
        except OSError:
            pass
    for candidate in (
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(r"D:\Steam"),
        Path(r"E:\Steam"),
    ):
        if (candidate / "userdata").is_dir():
            return candidate
    return None


def _contains_items(path: Path, *, directories: bool) -> bool:
    try:
        return any(ITEM_RE.match(entry.name) for entry in path.iterdir() if entry.is_dir() == directories)
    except OSError:
        return False


def discover_save_roots() -> list[SaveRoot]:
    found: list[SaveRoot] = []
    seen: set[str] = set()

    steam = _steam_root()
    if steam:
        userdata = steam / "userdata"
        try:
            accounts = [entry for entry in userdata.iterdir() if entry.is_dir() and entry.name.isdigit()]
        except OSError:
            accounts = []
        for account in accounts:
            remote = account / str(FH6_STEAM_APP_ID) / "remote"
            if not remote.is_dir():
                continue
            candidates = [remote]
            try:
                candidates.extend(entry for entry in remote.iterdir() if entry.is_dir())
            except OSError:
                pass
            selected = next((path for path in candidates if _contains_items(path, directories=False)), remote)
            key = os.path.normcase(str(selected.resolve()))
            if key not in seen:
                seen.add(key)
                found.append(SaveRoot("steam", account.name[-4:], selected.resolve()))

    for drive in _logical_drives():
        pgs = drive / "XboxGames" / "GameSave" / "pgs"
        if not pgs.is_dir():
            continue
        try:
            accounts = sorted(entry for entry in pgs.glob("u_*") if entry.is_dir())
        except OSError:
            continue
        for account in accounts:
            current = account / "current"
            snapshots: Iterable[Path]
            if current.exists():
                snapshots = [current]
            else:
                try:
                    snapshots = sorted(
                        (entry for entry in account.iterdir() if entry.is_dir() and entry.name.isdigit()),
                        key=lambda p: int(p.name),
                        reverse=True,
                    )
                except OSError:
                    snapshots = []
            for snapshot in snapshots:
                root = snapshot / "ContainersRoot"
                try:
                    resolved = root.resolve(strict=True)
                except OSError:
                    continue
                if not _contains_items(resolved, directories=True):
                    continue
                key = os.path.normcase(str(resolved))
                if key in seen:
                    break
                seen.add(key)
                found.append(SaveRoot("pgs", account.name[-6:], resolved))
                break

    return found


def _timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return None


def scan_save_root(root: SaveRoot) -> list[SaveItem]:
    items: dict[str, SaveItem] = {}
    try:
        entries = list(root.path.iterdir())
    except OSError:
        return []

    for entry in entries:
        match = ITEM_RE.match(entry.name)
        if not match:
            continue
        item_type, car_id, ts, part = match.groups()
        if entry.is_dir():
            item = SaveItem(root, entry.name, item_type, int(car_id), _timestamp(ts), True)
            try:
                item.files = {inner.name: inner for inner in entry.iterdir() if inner.is_file()}
            except OSError:
                pass
            items[item.base] = item
        elif entry.is_file():
            base = entry.name[: -(len(part) + 1)] if part else entry.name
            item = items.setdefault(
                base, SaveItem(root, base, item_type, int(car_id), _timestamp(ts), False)
            )
            item.files[part or ""] = entry

    for item in items.values():
        header = item.files.get("header")
        if header:
            try:
                item.header = parse_header(header.read_bytes())
                item.header_status = (
                    f"可读（已知变体 {item.header.section_marker.hex(' ')}）"
                )
            except UnsupportedHeaderVariantError as exc:
                item.header_status = "不支持的未知变体"
                item.header_error = str(exc)
            except HeaderTruncatedError as exc:
                item.header_status = "不可读（截断或损坏）"
                item.header_error = str(exc)
            except (OSError, ValueError, RuntimeError) as exc:
                item.header_status = "不可读（损坏）"
                item.header_error = f"header 读取或解析失败：{exc}"
        else:
            item.header_status = "缺少文件"
            item.header_error = "缺少 header 文件"

        livery = item.files.get("C_livery")
        if livery:
            try:
                item.livery = inspect_livery(livery.read_bytes())
            except (OSError, ValueError, RuntimeError) as exc:
                item.livery_error = f"C_livery 读取或解析失败：{exc}"
        elif item.item_type == "Livery":
            item.livery_error = "缺少 C_livery 文件"

        ids = {"directory": item.car_id}
        if item.header is not None:
            ids["header"] = item.header.car_id
        if item.livery is not None:
            ids["C_livery"] = item.livery.car_id
        if len(set(ids.values())) > 1:
            rendered = "、".join(f"{key}={value}" for key, value in ids.items())
            item.car_id_error = f"车型 ID 不一致：{rendered}"
    return sorted(items.values(), key=lambda value: value.timestamp or datetime.min.astimezone(), reverse=True)


def complete_liveries(root: SaveRoot) -> list[SaveItem]:
    return [item for item in scan_save_root(root) if item.complete_livery]


def seed_candidates(items: Iterable[SaveItem]) -> list[SaveItem]:
    return [
        item
        for item in items
        if item.complete_livery
        and item.header is not None
        and item.livery is not None
        and not item.header.published
        and not item.car_id_error
    ]


class CarTable:
    def __init__(self) -> None:
        candidates = [
            resource_root() / "assets" / "cars.json",
            resource_root() / "third_party" / "fh6-livery-viewer" / "cars.json",
        ]
        self.data: dict = {}
        for path in candidates:
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                pass

    def name(self, car_id: int) -> str:
        return self.data.get("fh6", {}).get(str(car_id), f"Car ID {car_id}")
