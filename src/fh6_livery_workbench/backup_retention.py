"""Project-wide rolling full-save backups. Metadata is never pruned.

A verified pre-import backup is retained until a newer import has passed its
readback. Pending/failed backups and explicit incidents hold pruning. Windows
archive deletion is performed by the bundled literal-path PowerShell helper.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SafetyError, WorkbenchError
from .util import app_root, json_dump_atomic, sha256_file, utc_now_iso

POLICY_FILE = ".livery-backup-policy.json"
POLICY_KIND = "livery-project-rolling-full-save-backups"


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _plain(path: Path, root: Path | None = None) -> Path:
    absolute = Path(os.path.abspath(path))
    resolved = absolute.resolve(strict=True)
    if absolute != resolved or (root is not None and not resolved.is_relative_to(root)):
        raise SafetyError(f"备份路径越界或经过重解析点：{path}")
    for part in (absolute, *absolute.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SafetyError(f"备份路径经过重解析点：{part}")
    return resolved


def policy_root(anchor: Path, explicit: Path | None = None, *,
                source: Path | None = None, isolated: bool = False) -> Path:
    def checked(candidate: Path) -> Path:
        target = candidate.resolve()
        if source is not None:
            save = source.resolve(strict=True)
            if target == save or target.is_relative_to(save) or (save.is_relative_to(target) and not isolated):
                raise SafetyError("项目备份区与实际存档重合；未创建备份策略或写入存档")
        return initialize(candidate)

    if explicit is not None:
        return checked(explicit)
    absolute = Path(anchor).absolute()
    for parent in (absolute, *absolute.parents):
        if (parent / POLICY_FILE).is_file():
            return checked(parent)
    settings = app_root() / "settings.local.json"
    if settings.is_file():
        configured = _read(settings).get("backup_policy_root")
        if configured:
            return checked(Path(configured))
    # Standalone Workbench's persistent workspace is its project by default.
    # Multi-job automation passes a common root or initializes its marker once.
    return checked(absolute.parent)


def initialize(project: Path) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    root = _plain(project)
    marker = root / POLICY_FILE
    if marker.exists():
        obj = _read(marker)
        if obj.get("kind") != POLICY_KIND or obj.get("keep_complete") != 1:
            raise SafetyError("项目备份策略不受支持，未执行清理")
    else:
        json_dump_atomic(marker, dict(schema=1, kind=POLICY_KIND, keep_complete=1,
            scope="whole-project-across-cars-and-jobs", assume_previous_ok_without_damage_report=True,
            prune_only_after_verified_import=True, preserve_incidents_and_failures=True,
            preserve_manifests_journals_and_validation=True))
    for name in ("archives", "records"):
        directory = root / ".livery-save-backups" / name
        directory.mkdir(parents=True, exist_ok=True)
        _plain(directory, root)
    return root


@contextmanager
def _locked(root: Path):
    path = root / ".livery-save-backups/state.lock"
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        deadline = time.monotonic() + 15
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise SafetyError("其他导入任务正在更新项目备份记录")
                time.sleep(.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _state(root: Path) -> dict[str, Any]:
    file = root / ".livery-save-backups/state.json"
    return _read(file) if file.exists() else dict(schema=1, current=None, backups={}, incidents=[])


def _write(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    json_dump_atomic(root / ".livery-save-backups/state.json", state)


def archive_directory(anchor: Path, explicit: Path | None = None, *,
                      source: Path | None = None, isolated: bool = False) -> tuple[Path, Path]:
    root = policy_root(anchor, explicit, source=source, isolated=isolated)
    return root, root / ".livery-save-backups/archives"


def verify_archive(manifest_path: Path) -> dict[str, Any]:
    manifest = _read(manifest_path)
    if manifest.get("kind") != "fh6-save-backup":
        raise SafetyError("不是完整存档备份清单")
    archive = Path(manifest["archive"])
    _plain(archive)
    if archive.suffix.lower() != ".zip" or sha256_file(archive) != manifest["archive_sha256"]:
        raise SafetyError("完整备份归档哈希不符")
    expected = {row["path"]: row for row in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise SafetyError("备份清单包含重复路径")
    with zipfile.ZipFile(archive) as check:
        entries = check.infolist()
        if len(entries) != len(expected) or {x.filename for x in entries} != set(expected):
            raise SafetyError("备份 ZIP 与清单文件集合不符")
        for member in entries:
            row = expected[member.filename]
            if member.file_size != row["size"]:
                raise SafetyError("备份 ZIP 文件大小不符")
            digest = hashlib.sha256()
            with check.open(member) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise SafetyError(f"备份内容校验不符：{member.filename}")
    return manifest


def register(manifest_path: Path, *, project: Path | None = None,
             adopted_success_note: str | None = None) -> dict[str, Any]:
    manifest_path = _plain(manifest_path)
    manifest = verify_archive(manifest_path)
    root = policy_root(manifest_path.parent, project)
    archive = _plain(Path(manifest["archive"]), root)
    source = Path(manifest["save_root"]).resolve()
    isolated = manifest.get("retention", {}).get("mode") == "isolated-test"
    if root == source or root.is_relative_to(source) or (source.is_relative_to(root) and not isolated):
        raise SafetyError("项目备份区与实际存档重合")
    key = hashlib.sha256(str(archive).encode("utf8")).hexdigest()[:24]
    with _locked(root):
        state = _state(root)
        old = state["backups"].get(key, {})
        if old.get("sha256") not in (None, manifest["archive_sha256"]):
            raise SafetyError("已登记的备份被替换")
        state["backups"][key] = dict(id=key, archive=str(archive), manifest=str(manifest_path),
            sha256=manifest["archive_sha256"], bytes=archive.stat().st_size,
            created_at=manifest["created_at"], status="succeeded" if adopted_success_note else "pending",
            note=adopted_success_note, verified_at=utc_now_iso())
        if adopted_success_note:
            state["backups"][key]["accepted_at"] = manifest["created_at"]
        _write(root, state)
    return dict(project_root=str(root), id=key)


def mark_incident(project: Path, reason: str, *, manifest: Path | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("保留事件需要说明")
    root = initialize(project)
    event = dict(id=uuid.uuid4().hex, reason=reason, opened_at=utc_now_iso(), resolved_at=None,
                 manifest=str(manifest) if manifest else None)
    with _locked(root):
        state = _state(root); state["incidents"].append(event)
        if manifest:
            for entry in state["backups"].values():
                if entry["manifest"] == str(manifest.resolve()):
                    entry["status"] = "failed"
        _write(root, state)
    return event


def resolve_incidents(project: Path, resolution: str) -> None:
    if not resolution.strip():
        raise ValueError("需记录问题处理及核验结果")
    root = initialize(project)
    with _locked(root):
        state = _state(root)
        for event in state["incidents"]:
            if not event["resolved_at"]:
                event.update(resolved_at=utc_now_iso(), resolution=resolution)
        # Once an incident is explicitly resolved, failed backups remain
        # available until a successful newer import authorizes rotation.
        for entry in state["backups"].values():
            if entry["status"] == "failed":
                entry["status"] = "resolved-failure"
        _write(root, state)


def finish_import(manifest_path: Path, journal_path: Path) -> dict[str, Any]:
    manifest = _read(manifest_path)
    root_hint = manifest.get("retention", {}).get("project_root")
    root = policy_root(manifest_path.parent, Path(root_hint) if root_hint else None)
    journal = _read(journal_path)
    if (journal.get("kind") != "fh6-additive-install-journal" or
            Path(journal["backup_manifest"]).resolve() != manifest_path.resolve() or
            journal.get("readback_status") != "ALL_NEW_AND_PREVIOUS_FILES_VERIFIED"):
        raise SafetyError("尚未完成本次导入及完整读回，不能淘汰旧备份")
    with _locked(root):
        state = _state(root)
        matches = [x for x in state["backups"].values() if x["manifest"] == str(manifest_path.resolve())]
        if len(matches) != 1:
            raise SafetyError("本次导入备份未登记")
        entry = matches[0]
        entry.update(status="succeeded", accepted_at=journal["installed_at"], journal=str(journal_path))
        state["current"] = entry["id"]
        _write(root, state)
    return prune(root)


def prune(project: Path, *, select_latest_adopted: bool = False) -> dict[str, Any]:
    root = initialize(project)
    with _locked(root):
        state = _state(root)
        if select_latest_adopted:
            candidates = [e for e in state["backups"].values() if e["status"] == "succeeded"]
            if not candidates:
                raise SafetyError("没有已核验且正常的完整备份可保留")
            state["current"] = max(candidates, key=lambda e: _instant(e["created_at"]))["id"]
        current = state["backups"].get(state["current"])
        if not current:
            raise SafetyError("没有成功导入对应的保留备份")
        held = [x for x in state["incidents"] if not x["resolved_at"]]
        if held:
            return dict(status="HELD_FOR_REPORTED_DAMAGE_OR_IMPORT_FAILURE", incidents=[x["id"] for x in held], deleted=[])
        # Verify the retained full archive before deleting any predecessor.
        verify_archive(Path(current["manifest"]))
        selected = [e for e in state["backups"].values() if e["id"] != current["id"] and
                    e["status"] in {"succeeded", "resolved-failure"} and
                    _instant(e["created_at"]) <= _instant(current["created_at"]) and Path(e["archive"]).exists()]
        rows = []
        for entry in selected:
            archive = _plain(Path(entry["archive"]), root)
            if archive == Path(current["archive"]) or archive.suffix.lower() != ".zip":
                raise SafetyError("清理目标不正确")
            if archive.stat().st_size != entry["bytes"] or sha256_file(archive) != entry["sha256"]:
                raise SafetyError("旧备份在清理前发生变化，已保留")
            rows.append(dict(path=str(archive), bytes=entry["bytes"], sha256=entry["sha256"], manifest=entry["manifest"]))
        token = uuid.uuid4().hex
        plan = root / f".livery-save-backups/records/prune-{token}.json"
        receipt = plan.with_name(f"prune-{token}.receipt.json")
        json_dump_atomic(plan, dict(kind="rolling-full-save-backup-prune", project_root=str(root),
            retained=current, entries=rows, preserve="All manifests, journals, validation, actual saves, sources and .3so products"))
        if rows:
            helper = Path(__file__).with_name("delete_backup_archives.ps1")
            if os.name == "nt":
                import shutil
                powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
                if not powershell:
                    raise WorkbenchError("缺少 PowerShell；备份保留，未静默改用其他删除方式")
                proc = subprocess.run([powershell,"-NoProfile","-NonInteractive","-File",str(helper),
                    "-PlanPath",str(plan),"-ReceiptPath",str(receipt)], shell=False,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0), capture_output=True,
                    text=True,encoding="utf-8",errors="replace")
                if proc.returncode:
                    raise WorkbenchError(f"备份清理未完成，详情保留在 {receipt}")
            else:
                # Public codec tests can run on non-Windows; the target OS
                # always takes the literal-path PowerShell route above.
                for row in rows:
                    archive = _plain(Path(row["path"]), root)
                    if sha256_file(archive) != row["sha256"]:
                        raise SafetyError("旧备份发生变化")
                    archive.unlink()
                json_dump_atomic(receipt, dict(status="PRUNED", deleted=[r["path"] for r in rows]))
        else:
            json_dump_atomic(receipt, dict(status="NO_OLDER_COMPLETED_BACKUPS",deleted=[]))
        for entry in selected:
            if not Path(entry["archive"]).exists():
                entry.update(status="pruned", pruned_at=utc_now_iso(), prune_receipt=str(receipt))
        _write(root, state)
        return dict(status="ROLLING_BACKUP_RETAINED", retained=current["archive"], deleted=[r["path"] for r in rows],
                    deletedBytes=sum(r["bytes"] for r in rows), plan=str(plan), receipt=str(receipt))


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root",type=Path,required=True)
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("init");commands.add_parser("status");commands.add_parser("prune")
    hold=commands.add_parser("hold");hold.add_argument("--reason",required=True)
    resolved=commands.add_parser("resolve");resolved.add_argument("--resolution",required=True)
    args=parser.parse_args();root=initialize(args.project_root)
    if args.command=="hold":result=mark_incident(root,args.reason)
    elif args.command=="resolve":resolve_incidents(root,args.resolution);result=_state(root)
    elif args.command=="prune":result=prune(root)
    else:result=dict(policy=_read(root/POLICY_FILE),state=_state(root))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
