from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .container_codec import inspect_livery
from .errors import SafetyError, WorkbenchError
from .header_codec import parse_header
from .process_guard import require_game_closed
from .save_scan import ITEM_RE, SaveRoot
from .util import json_dump_atomic, local_stamp, relative_manifest, sha256_file, utc_now_iso
from . import backup_retention as retention


def active_root(save_root: SaveRoot) -> Path:
    if save_root.platform != "pgs":
        return save_root.path.resolve(strict=True)
    snapshot = save_root.path.parent
    account = snapshot.parent
    current = account / "current" / "ContainersRoot"
    try:
        return current.resolve(strict=True)
    except OSError as exc:
        raise SafetyError("无法重新解析 PGS current 活跃快照") from exc


def _all_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _create_backup(save_root: SaveRoot, backup_directory: Path, archive_directory: Path) -> Path:
    require_game_closed()
    source = active_root(save_root)
    backup_directory.mkdir(parents=True, exist_ok=True)
    stem = f"fh6_{save_root.platform}_{local_stamp()}_{uuid.uuid4().hex[:8]}"
    archive_directory.mkdir(parents=True, exist_ok=True)
    archive = archive_directory / f"{stem}.zip"
    manifest_path = backup_directory / f"{stem}.manifest.json"
    if archive.exists() or manifest_path.exists():
        raise WorkbenchError("备份文件名冲突，请一秒后重试")
    files = _all_files(source)
    entries = relative_manifest(source, files)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
        for file in files:
            output.write(file, file.relative_to(source).as_posix())
    with zipfile.ZipFile(archive, "r") as check:
        bad = check.testzip()
        if bad:
            raise WorkbenchError(f"备份 ZIP CRC 校验失败：{bad}")
        if set(check.namelist()) != {entry["path"] for entry in entries}:
            raise WorkbenchError("备份 ZIP 文件清单回读不一致")
    json_dump_atomic(
        manifest_path,
        {
            "schema": 1,
            "kind": "fh6-save-backup",
            "created_at": utc_now_iso(),
            "platform": save_root.platform,
            "save_root": str(source),
            "archive": str(archive),
            "archive_sha256": sha256_file(archive),
            "files": entries,
        },
    )
    return manifest_path


def backup_save(save_root: SaveRoot, backup_directory: Path, *, policy_project: Path | None = None) -> Path:
    """Create and content-verify a pending backup; never prune at creation."""
    require_game_closed()
    source = active_root(save_root)
    marker = source.parent.parent / ".livery-workflow-sandbox.json"
    isolated = marker.is_file() and _load_json(marker).get("kind") == "livery-workflow-isolated-import-test"
    root, archive_directory = retention.archive_directory(backup_directory, policy_project,
                                                         source=source, isolated=isolated)
    manifest_path = None
    try:
        manifest_path = _create_backup(save_root, backup_directory, archive_directory)
        manifest = _load_json(manifest_path)
        source = Path(manifest["save_root"])
        manifest["retention"] = dict(project_root=str(root), mode="isolated-test" if isolated else "live-project",
                                     keep_complete=1, stage="pending-import")
        json_dump_atomic(manifest_path, manifest)
        # Includes ZIP CRC, exact member set, sizes and every content SHA-256.
        retention.register(manifest_path, project=root)
        for row in manifest["files"]:
            path = source / row["path"]
            if path.stat().st_size != row["size"] or sha256_file(path) != row["sha256"]:
                raise SafetyError("创建完整备份期间存档发生变化，备份全部保留")
        return manifest_path
    except Exception as exc:
        retention.mark_incident(root, f"完整备份创建/核验失败：{exc}", manifest=manifest_path)
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"无法读取清单 {path.name}: {exc}") from exc


def validate_ready(ready: Path) -> dict[str, Any]:
    manifest = _load_json(ready / "workbench-ready.json")
    if manifest.get("kind") != "fh6-private-livery-ready":
        raise WorkbenchError("目录不是 Workbench 待安装涂装")
    if not ITEM_RE.match(ready.name) or not ready.name.startswith("Livery_"):
        raise WorkbenchError("待安装目录名不是标准 Livery_<car>_<timestamp>")
    header_path = ready / "header"
    payload_path = ready / "C_livery"
    if not header_path.is_file() or not payload_path.is_file():
        raise WorkbenchError("待安装目录缺少 header 或 C_livery")
    header = parse_header(header_path.read_bytes())
    livery = inspect_livery(payload_path.read_bytes())
    if header.published or livery.source_state != 0:
        raise WorkbenchError("待安装涂装不是私人草稿状态")
    if header.car_id != livery.car_id:
        raise WorkbenchError("待安装 header 与 C_livery 车型不一致")
    expected = manifest.get("files", {})
    for name, digest in expected.items():
        file = ready / name
        if not file.is_file() or sha256_file(file) != digest:
            raise WorkbenchError(f"待安装文件哈希不一致：{name}")
    return manifest


def _verify_backup(backup_manifest: Path, save_root: SaveRoot) -> dict[str, Any]:
    backup = _load_json(backup_manifest)
    if backup.get("kind") != "fh6-save-backup":
        raise SafetyError("所选文件不是 Workbench 存档备份清单")
    active = active_root(save_root)
    if os.path.normcase(backup.get("save_root", "")) != os.path.normcase(str(active)):
        raise SafetyError("备份对应的 PGS 快照已不是当前活跃快照，请重新备份")
    archive = Path(backup.get("archive", ""))
    if not archive.is_file() or sha256_file(archive) != backup.get("archive_sha256"):
        raise SafetyError("备份 ZIP 不存在或哈希验证失败")
    return backup


@dataclass(slots=True)
class InstallPlan:
    platform: str
    active_root: Path
    ready: Path
    base: str
    destinations: list[Path]
    source_hashes: dict[str, str]
    backup_manifest: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "active_root": str(self.active_root),
            "ready": str(self.ready),
            "base": self.base,
            "destinations": [str(path) for path in self.destinations],
            "source_hashes": self.source_hashes,
            "backup_manifest": str(self.backup_manifest),
            "operation": "add-only",
            "overwrites": 0,
        }


def _plan_install(ready: Path, save_root: SaveRoot, backup_manifest: Path) -> InstallPlan:
    require_game_closed()
    validate_ready(ready)
    _verify_backup(backup_manifest, save_root)
    root = active_root(save_root)
    payload_files = [path for path in ready.iterdir() if path.is_file() and path.name != "workbench-ready.json"]
    hashes = {path.name: sha256_file(path) for path in payload_files}
    if save_root.platform == "pgs":
        destinations = [root / ready.name]
    elif save_root.platform == "steam":
        destinations = [root / f"{ready.name}.{file.name}" for file in payload_files]
    else:
        raise WorkbenchError(f"不支持的存档平台：{save_root.platform}")
    collisions = [path for path in destinations if path.exists()]
    if collisions:
        raise SafetyError("新增目标已存在，拒绝覆盖：" + ", ".join(path.name for path in collisions))
    return InstallPlan(
        platform=save_root.platform,
        active_root=root,
        ready=ready,
        base=ready.name,
        destinations=destinations,
        source_hashes=hashes,
        backup_manifest=backup_manifest,
    )


def _hold_failed_import(manifest: Path, error: Exception) -> None:
    try:
        backup = _load_json(manifest)
        project = backup.get("retention", {}).get("project_root")
        if project:
            retention.mark_incident(Path(project), f"导入失败，暂停备份淘汰：{error}", manifest=manifest)
    except Exception:
        # Do not hide the original import error. No prune is invoked here.
        pass


def plan_install(ready: Path, save_root: SaveRoot, backup_manifest: Path) -> InstallPlan:
    try:
        return _plan_install(ready, save_root, backup_manifest)
    except Exception as exc:
        _hold_failed_import(backup_manifest, exc)
        raise


def _execute_install(plan: InstallPlan, journal_directory: Path, *, confirmation: str) -> Path:
    if confirmation != "INSTALL":
        raise SafetyError("安装确认词不正确")
    require_game_closed()
    if any(path.exists() for path in plan.destinations):
        raise SafetyError("目标在干跑后出现，已拒绝覆盖")
    journal_directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    temporary_paths: list[Path] = []
    try:
        if plan.platform == "pgs":
            final = plan.destinations[0]
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{plan.base}.fh6lw-", dir=plan.active_root)
            )
            temporary_paths.append(temporary)
            for name, digest in plan.source_hashes.items():
                source = plan.ready / name
                target = temporary / name
                shutil.copy2(source, target)
                if sha256_file(target) != digest:
                    raise WorkbenchError(f"临时写入回读失败：{name}")
            os.replace(temporary, final)
            temporary_paths.remove(temporary)
            written.append(final)
        else:
            for destination in plan.destinations:
                part = destination.name[len(plan.base) + 1 :]
                source = plan.ready / part
                temporary = destination.with_name(destination.name + ".fh6lw-tmp")
                temporary_paths.append(temporary)
                shutil.copy2(source, temporary)
                if sha256_file(temporary) != plan.source_hashes[part]:
                    raise WorkbenchError(f"临时写入回读失败：{part}")
                os.replace(temporary, destination)
                temporary_paths.remove(temporary)
                written.append(destination)
    except Exception:
        for path in temporary_paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
        for path in written:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
        raise

    installed_hashes: dict[str, str] = {}
    if plan.platform == "pgs":
        for name, digest in plan.source_hashes.items():
            target = plan.destinations[0] / name
            actual = sha256_file(target)
            if actual != digest:
                raise WorkbenchError(f"安装后回读失败：{name}")
            installed_hashes[str(target)] = actual
    else:
        for target in plan.destinations:
            actual = sha256_file(target)
            part = target.name[len(plan.base) + 1 :]
            if actual != plan.source_hashes[part]:
                raise WorkbenchError(f"安装后回读失败：{target.name}")
            installed_hashes[str(target)] = actual

    journal = journal_directory / f"install_{local_stamp()}_{plan.base}.json"
    json_dump_atomic(
        journal,
        {
            "schema": 1,
            "kind": "fh6-additive-install-journal",
            "installed_at": utc_now_iso(),
            "platform": plan.platform,
            "active_root": str(plan.active_root),
            "base": plan.base,
            "backup_manifest": str(plan.backup_manifest),
            "installed": installed_hashes,
            "rolled_back_at": None,
        },
    )
    return journal


def execute_install(plan: InstallPlan, journal_directory: Path, *, confirmation: str,
                    finalize_backup: bool = True) -> Path:
    """Only a fully read-back import may rotate old complete save backups."""
    try:
        save = SaveRoot(plan.platform, "import-readback", plan.active_root)
        before = _verify_backup(plan.backup_manifest, save)
        # Reject a stale backup before writing, rather than discovering it late.
        expected_before = {row["path"] for row in before["files"]}
        actual_before = {p.relative_to(plan.active_root).as_posix() for p in _all_files(plan.active_root)}
        if actual_before != expected_before:
            raise SafetyError("备份后存档文件集合已变化，请重新备份")
        for row in before["files"]:
            p = plan.active_root / row["path"]
            if p.stat().st_size != row["size"] or sha256_file(p) != row["sha256"]:
                raise SafetyError("备份后存档内容已变化，请重新备份")
        journal = _execute_install(plan, journal_directory, confirmation=confirmation)
        require_game_closed()
        if active_root(save) != plan.active_root:
            raise SafetyError("导入期间活动存档根发生变化")
        for row in before["files"]:
            p = plan.active_root / row["path"]
            if p.stat().st_size != row["size"] or sha256_file(p) != row["sha256"]:
                raise SafetyError("旧存档文件回读不一致；完整备份保留")
        added = ({f"{plan.base}/{name}" for name in plan.source_hashes} if plan.platform == "pgs"
                 else {p.name for p in plan.destinations})
        actual = {p.relative_to(plan.active_root).as_posix() for p in _all_files(plan.active_root)}
        if actual != expected_before | added:
            raise SafetyError("导入后存在意外新增或移除文件；完整备份保留")
        obj = _load_json(journal)
        obj.update(readback_status="ALL_NEW_AND_PREVIOUS_FILES_VERIFIED",
                   prior_files_preserved=len(expected_before), active_root_stable=True)
        json_dump_atomic(journal, obj)
    except Exception as exc:
        _hold_failed_import(plan.backup_manifest, exc)
        raise
    if finalize_backup:
        try:
            result = retention.finish_import(plan.backup_manifest, journal)
        except Exception as exc:
            # The game import succeeded. A locked old archive / pruning error
            # must retain the backups, not masquerade as a failed game import.
            result = dict(status="RETENTION_CLEANUP_DEFERRED_BACKUPS_RETAINED", error=str(exc))
        obj = _load_json(journal); obj["backup_retention"] = result; json_dump_atomic(journal, obj)
    return journal


def rollback_install(journal_path: Path, *, confirmation: str) -> None:
    if confirmation != "ROLLBACK":
        raise SafetyError("回滚确认词不正确")
    require_game_closed()
    journal = _load_json(journal_path)
    if journal.get("kind") != "fh6-additive-install-journal":
        raise SafetyError("文件不是 Workbench 安装日志")
    if journal.get("rolled_back_at"):
        raise SafetyError("该安装已经回滚")
    installed = journal.get("installed", {})
    paths = [Path(value) for value in installed]
    for path in paths:
        if path.is_dir():
            for file in path.iterdir():
                if file.is_file():
                    expected = installed.get(str(file))
                    if expected and sha256_file(file) != expected:
                        raise SafetyError(f"已安装内容被修改，拒绝删除：{file.name}")
        elif not path.is_file() or sha256_file(path) != installed[str(path)]:
            raise SafetyError(f"已安装内容不存在或被修改，拒绝删除：{path.name}")
    # Exact paths come only from the journal and were hash-verified above.
    if journal.get("platform") == "pgs":
        folder = Path(journal["active_root"]) / journal["base"]
        if folder.exists():
            shutil.rmtree(folder)
    else:
        for path in paths:
            path.unlink()
    journal["rolled_back_at"] = utc_now_iso()
    json_dump_atomic(journal_path, journal)
