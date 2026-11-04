from pathlib import Path
import json, gzip
import pytest
from fh6_livery_workbench.cli import _parser,main
from fh6_livery_workbench.staging import stage_export
from fh6_livery_workbench.installer import backup_save,plan_install,execute_install,rollback_install
from fh6_livery_workbench.readback import verify_install
from fh6_livery_workbench.save_scan import SaveRoot,SaveItem
from fh6_livery_workbench.header_codec import parse_header
from helpers import draft_header,livery

COMMANDS={"convert-painter","validate-project","validate-native","stage","backup",
          "backup-policy","validate-ready","dry-run-install","install","readback","rollback"}

def test_command_surface():
    sub=next(a for a in _parser()._actions if hasattr(a,"choices") and isinstance(a.choices,dict))
    assert set(sub.choices) == COMMANDS

@pytest.mark.parametrize("version",[3,4])
def test_authored_project_versions(tmp_path,version):
    p=tmp_path/"artwork.3so"
    with gzip.open(p,"wt",encoding="utf8") as f:
        json.dump(dict(format="fls_editor_project",version=version,root=dict(children=[])),f)
    assert main(["validate-project",str(p)]) == 0

def test_stage_install_readback_rollback(tmp_path,monkeypatch):
    monkeypatch.setattr("fh6_livery_workbench.installer.require_game_closed",lambda:None)
    root=tmp_path/"save";root.mkdir()
    (root/"existing.txt").write_text("must survive")
    save=SaveRoot("steam","synthetic",root)
    seed=SaveItem(save_root=save,base="own-seed",item_type="Livery",car_id=1234,
                  timestamp="",is_directory=True)
    header=draft_header("Own",1234,"Synthetic",b"OWN_TAG!")
    seed.header=parse_header(header)
    export=tmp_path/"export";export.mkdir()
    (export/"header").write_bytes(header);(export/"C_livery").write_bytes(livery(1234,b"OWN_TAG!",0))
    ready=stage_export(export,seed,tmp_path/"work",final_name="Authored")
    backup=backup_save(save,tmp_path/"backup",policy_project=tmp_path/"policy")
    plan=plan_install(ready,save,backup)
    journal=execute_install(plan,tmp_path/"journals",confirmation="INSTALL")
    assert verify_install(journal)["filesVerified"] == 2
    assert (root/"existing.txt").read_text() == "must survive"
    installed=json.loads(journal.read_text())["installed"]
    changed=Path(next(iter(installed)));changed.write_bytes(b"changed")
    with pytest.raises(Exception,match="mismatch"):verify_install(journal)
    part=changed.name[len(ready.name)+1:];changed.write_bytes((ready/part).read_bytes())
    assert verify_install(journal)["status"] == "RECORDED_INSTALLATION_VERIFIED"
    rollback_install(journal,confirmation="ROLLBACK")
    assert list(root.iterdir()) == [root/"existing.txt"]

def test_readback_rejects_path_escape(tmp_path):
    root=tmp_path/"save";root.mkdir()
    outside=tmp_path/"outside";outside.write_text("private")
    journal=tmp_path/"journal.json"
    journal.write_text(json.dumps(dict(kind="fh6-additive-install-journal",platform="pgs",
        active_root=str(root),base="Livery_7_20261104010101",installed={str(outside):"invalid"})))
    with pytest.raises(Exception,match="mismatch"):verify_install(journal)
