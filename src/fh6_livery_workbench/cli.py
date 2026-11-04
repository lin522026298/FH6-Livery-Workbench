"""Headless conversion, validation and file-only import of authored liveries."""
from pathlib import Path
import argparse, json, sys
from . import __version__
from .errors import WorkbenchError
from .container_codec import inspect_livery
from .header_codec import parse_header
from .installer import backup_save, validate_ready, plan_install, execute_install, rollback_install
from .project3so import load_project, painter_shapes_to_project
from .readback import verify_install
from .save_scan import SaveRoot, SaveItem
from .staging import stage_export

def _parser():
    p=argparse.ArgumentParser(prog="fh6-livery-workbench",description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--workspace",type=Path,default=Path.cwd()/"workspace")
    p.add_argument("--project-root",type=Path,help="Common project backup-retention root")
    sub=p.add_subparsers(dest="command",required=True)
    cv=sub.add_parser("convert-painter",help="Convert authored painter-shapes-v1 into a Group .3so")
    cv.add_argument("source",type=Path);cv.add_argument("output",type=Path)
    for cmd,field in [("validate-project","project"),("validate-native","payload"),
                      ("validate-ready","ready"),("readback","journal")]:
        sp=sub.add_parser(cmd);sp.add_argument(field,type=Path)
    stage=sub.add_parser("stage",help="Stage an authored native export for additive installation")
    stage.add_argument("export",type=Path);stage.add_argument("--seed-header",type=Path,required=True)
    stage.add_argument("--name")
    backup=sub.add_parser("backup",help="Create and verify a complete save backup")
    for sp in (stage,backup):
        sp.add_argument("--save-root",type=Path,required=True)
        sp.add_argument("--platform",choices=["pgs","steam"],required=True)
    policy=sub.add_parser("backup-policy",help="Project rolling retention and incident holds")
    policy.add_argument("action",choices=["init","status","hold","resolve","prune"])
    policy.add_argument("--reason")
    for cmd in ("dry-run-install","install"):
        sp=sub.add_parser(cmd);sp.add_argument("ready",type=Path)
        sp.add_argument("backup_manifest",type=Path)
        sp.add_argument("--save-root",type=Path,required=True)
        sp.add_argument("--platform",choices=["pgs","steam"],required=True)
        if cmd == "install":sp.add_argument("--confirm",required=True)
    rb=sub.add_parser("rollback",help="Roll back only entries recorded in an own-install journal")
    rb.add_argument("journal",type=Path);rb.add_argument("--confirm",required=True)
    return p

def _save(a):return SaveRoot(a.platform,"explicit-local-save",a.save_root.resolve(strict=True))

def run(a):
    if a.command == "convert-painter":return painter_shapes_to_project(a.source,a.output)
    if a.command == "validate-project":
        project=load_project(a.project)
        return dict(status="PROJECT_VALID",format=project["format"],version=project["version"])
    if a.command == "validate-native":
        info=inspect_livery(a.payload.read_bytes())
        return dict(status="NATIVE_CONTAINER_VALID",carId=info.car_id,sourceState=info.source_state)
    if a.command == "validate-ready":return validate_ready(a.ready)
    if a.command == "readback":return verify_install(a.journal)
    if a.command == "stage":
        seed=SaveItem(save_root=_save(a),base=a.seed_header.parent.name,item_type="Livery",
                      car_id=0,timestamp="",is_directory=True)
        seed.header=parse_header(a.seed_header.read_bytes())
        return dict(ready=str(stage_export(a.export,seed,a.workspace,final_name=a.name)))
    if a.command == "backup":
        if a.project_root is None:raise WorkbenchError("Specify the common --project-root")
        return dict(backupManifest=str(backup_save(_save(a),a.workspace/"backups",policy_project=a.project_root)))
    if a.command == "backup-policy":
        from . import backup_retention as r
        if a.project_root is None:raise WorkbenchError("Specify the common --project-root")
        root=r.initialize(a.project_root)
        if a.action == "hold":return r.mark_incident(root,a.reason or "User reported save damage")
        if a.action == "resolve":r.resolve_incidents(root,a.reason or "")
        if a.action == "prune":return r.prune(root)
        return dict(policy=str(root/r.POLICY_FILE),state=r._state(root))
    if a.command in ("install","dry-run-install"):
        plan=plan_install(a.ready,_save(a),a.backup_manifest)
        if a.command == "dry-run-install":return plan.to_dict()
        journal=execute_install(plan,a.workspace/"journals",confirmation=a.confirm)
        return dict(journal=str(journal),readback=verify_install(journal))
    if a.command == "rollback":
        rollback_install(a.journal,confirmation=a.confirm);return dict(status="ROLLBACK_VERIFIED")
    raise WorkbenchError("Unknown command")

def main(argv=None):
    try:
        result=run(_parser().parse_args(argv))
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    except (WorkbenchError,OSError,ValueError,KeyError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr);return 2

if __name__ == "__main__":raise SystemExit(main())
