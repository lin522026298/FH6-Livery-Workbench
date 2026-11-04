from __future__ import annotations

import json
from pathlib import Path

import pytest

from fh6_livery_workbench import backup_retention as retention
from fh6_livery_workbench.errors import SafetyError, WorkbenchError
from fh6_livery_workbench.installer import backup_save, execute_install, plan_install
from fh6_livery_workbench.save_scan import SaveRoot
from fh6_livery_workbench.util import sha256_file
from helpers import draft_header, livery


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ready(project: Path, index: int, car: int = 1234) -> Path:
    directory = project / "ready" / f"Livery_{car}_{20261104000000 + index}"
    directory.mkdir(parents=True)
    (directory / "header").write_bytes(draft_header("Synthetic", car, "Fixture", b"LOCAL123"))
    (directory / "C_livery").write_bytes(livery(car, b"LOCAL123", 0))
    (directory / "workbench-ready.json").write_text(json.dumps(dict(kind="fh6-private-livery-ready",
        files={n: sha256_file(directory / n) for n in ("header", "C_livery")})), encoding="utf8")
    return directory


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr("fh6_livery_workbench.installer.require_game_closed", lambda: None)
    source = tmp_path / "synthetic-steam-save"; source.mkdir()
    (source / "progress.bin").write_bytes(b"ORIGINAL-PROGRESS" * 200)
    project = tmp_path / "common-project"; project.mkdir()
    return SaveRoot("steam", "synthetic", source), project


def imported(save: SaveRoot, project: Path, index: int, *, car: int = 1234):
    manifest = backup_save(save, project / f"job-{index}/backup", policy_project=project)
    plan = plan_install(ready(project, index, car), save, manifest)
    journal = execute_install(plan, project / f"job-{index}/journal", confirmation="INSTALL")
    return manifest, journal


def test_misconfigured_project_in_save_is_rejected_before_policy_write(environment):
    save, project = environment
    with pytest.raises(SafetyError, match="重合"):
        backup_save(save, project / "bad/backup", policy_project=save.path)
    assert not (save.path / retention.POLICY_FILE).exists()
    assert [p.name for p in save.path.iterdir()] == ["progress.bin"]


def test_prune_failure_is_deferred_without_losing_success_or_backups(environment, monkeypatch):
    save, project = environment
    first, _ = imported(save, project, 1)
    def unavailable(*args, **kwargs):
        raise WorkbenchError("Synthetic locked old archive")
    with monkeypatch.context() as patch:
        patch.setattr(retention.subprocess, "run", unavailable)
        second, journal = imported(save, project, 2)
    result = read(journal)
    assert result["readback_status"] == "ALL_NEW_AND_PREVIOUS_FILES_VERIFIED"
    assert result["backup_retention"]["status"].startswith("RETENTION_CLEANUP_DEFERRED")
    assert all(Path(read(p)["archive"]).exists() for p in (first, second))
    assert read(project / ".livery-save-backups/state.json")["incidents"] == []
    assert len(retention.prune(project)["deleted"]) == 1


def test_new_backup_is_verified_before_old_is_pruned_across_jobs_and_cars(environment):
    save, project = environment
    progress = sha256_file(save.path / "progress.bin")
    first, first_journal = imported(save, project, 1)
    first_archive = Path(read(first)["archive"])
    second = backup_save(save, project / "another-car/backup", policy_project=project)
    second_archive = Path(read(second)["archive"])
    assert first_archive.exists() and second_archive.exists()  # no premature deletion
    retention.verify_archive(second)
    plan = plan_install(ready(project, 2, 999), save, second)
    second_journal = execute_install(plan, project / "another-car/journal", confirmation="INSTALL")
    assert not first_archive.exists() and second_archive.exists()
    assert first.is_file() and first_journal.is_file() and second_journal.is_file()
    assert len(list((project / ".livery-save-backups/archives").glob("*.zip"))) == 1
    assert read(second_journal)["readback_status"] == "ALL_NEW_AND_PREVIOUS_FILES_VERIFIED"
    assert sha256_file(save.path / "progress.bin") == progress


def test_failed_import_holds_backups_until_resolved_and_new_import_succeeds(environment):
    save, project = environment
    first, _ = imported(save, project, 1)
    failed = backup_save(save, project / "failed/backup", policy_project=project)
    collision = next((project / "ready").iterdir())
    with pytest.raises(SafetyError):
        plan_install(collision, save, failed)
    assert Path(read(first)["archive"]).exists() and Path(read(failed)["archive"]).exists()
    held, journal = imported(save, project, 3)
    assert read(journal)["backup_retention"]["status"].startswith("HELD")
    assert all(Path(read(p)["archive"]).exists() for p in (first, failed, held))
    retention.resolve_incidents(project, "Synthetic collision diagnosed; no damage, ready target corrected")
    # Resolving alone is not permission to discard a newer failed preimage.
    retention.prune(project)
    assert Path(read(held)["archive"]).exists()
    last, _ = imported(save, project, 4)
    assert Path(read(last)["archive"]).exists()
    assert all(not Path(read(p)["archive"]).exists() for p in (first, failed, held))
    assert all(p.is_file() for p in (first, failed, held))


def test_reported_damage_freezes_rotation_without_a_reconfirmation_prompt(environment, monkeypatch):
    save, project = environment
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Unexpected confirmation"))
    first, _ = imported(save, project, 1)
    retention.mark_incident(project, "Synthetic user reports save damage")
    second, journal = imported(save, project, 2)
    assert read(journal)["backup_retention"]["status"].startswith("HELD")
    assert all(Path(read(p)["archive"]).exists() for p in (first, second))
    retention.resolve_incidents(project, "Synthetic restore/readback confirms repaired content")
    third, _ = imported(save, project, 3)
    assert Path(read(third)["archive"]).exists()
    assert all(not Path(read(p)["archive"]).exists() for p in (first, second))


def test_bad_new_backup_never_deletes_old_good_one(environment, monkeypatch):
    save, project = environment
    first, _ = imported(save, project, 1)
    original = retention.verify_archive
    def corrupt(path):
        archive = Path(read(path)["archive"])
        archive.write_bytes(b"INTENTIONALLY CORRUPTED TEST ARCHIVE")
        return original(path)
    monkeypatch.setattr(retention, "verify_archive", corrupt)
    with pytest.raises(SafetyError):
        backup_save(save, project / "bad/backup", policy_project=project)
    monkeypatch.setattr(retention, "verify_archive", original)
    original(first)
    assert retention.prune(project)["status"].startswith("HELD")


def test_changed_save_after_backup_fails_before_writing_and_preserves_all_backups(environment):
    save, project = environment
    first, _ = imported(save, project, 1)
    second = backup_save(save, project / "changed/backup", policy_project=project)
    target = ready(project, 2)
    plan = plan_install(target, save, second)
    (save.path / "progress.bin").write_bytes(b"Changed independently after backup")
    with pytest.raises(SafetyError):
        execute_install(plan, project / "changed/journal", confirmation="INSTALL")
    assert not list(save.path.glob(target.name + ".*"))
    assert all(Path(read(p)["archive"]).exists() for p in (first, second))


def test_refuse_cleanup_outside_project_even_if_state_is_tampered(environment, tmp_path):
    save, project = environment
    first, _ = imported(save, project, 1)
    second = backup_save(save, project / "next/backup", policy_project=project)
    plan = plan_install(ready(project, 2), save, second)
    journal = execute_install(plan, project / "next/journal", confirmation="INSTALL", finalize_backup=False)
    outside = tmp_path / "unrelated.zip"; outside.write_bytes(b"KEEP OUTSIDE")
    state_path = project / ".livery-save-backups/state.json"
    state = read(state_path)
    old = next(e for e in state["backups"].values() if e["manifest"] == str(first.resolve()))
    old.update(archive=str(outside), bytes=outside.stat().st_size, sha256=sha256_file(outside))
    state_path.write_text(json.dumps(state), encoding="utf8")
    with pytest.raises(SafetyError):
        retention.finish_import(second, journal)
    assert outside.read_bytes() == b"KEEP OUTSIDE"
    assert Path(read(first)["archive"]).exists() and Path(read(second)["archive"]).exists()
