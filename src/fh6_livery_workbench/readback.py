"""Verify files recorded by this tool's own additive-install journal."""
from pathlib import Path
import json
from .errors import SafetyError
from .util import sha256_file

def verify_install(journal_path: Path) -> dict:
    journal=json.loads(journal_path.read_text(encoding="utf8"))
    if journal.get("kind") != "fh6-additive-install-journal" or journal.get("rolled_back_at"):
        raise SafetyError("Expected an active Workbench installation journal")
    root=Path(journal["active_root"]).resolve(strict=True)
    base=journal["base"]
    if not isinstance(base,str) or Path(base).name != base or not base.startswith("Livery_"):
        raise SafetyError("Invalid journal item name")
    installed=journal.get("installed",{})
    if not installed:
        raise SafetyError("Journal contains no installed files")
    for name,expected in installed.items():
        path=Path(name)
        resolved=path.resolve(strict=True)
        if path.is_symlink() or resolved != path.absolute() or not resolved.is_file():
            raise SafetyError("Installed file boundary changed")
        if journal["platform"] == "pgs":
            permitted=resolved.parent == root/base
        elif journal["platform"] == "steam":
            permitted=resolved.parent == root and resolved.name.startswith(base+".")
        else:
            raise SafetyError("Unknown installation platform")
        if not permitted or sha256_file(resolved) != expected:
            raise SafetyError("Installed file boundary or hash mismatch")
    return {"status":"RECORDED_INSTALLATION_VERIFIED", "filesVerified":len(installed),
            "base":base, "gameStarted":False}
