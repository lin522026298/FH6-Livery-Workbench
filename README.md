# FH6 Livery Workbench

Headless Python tools for authored Forza Horizon 6 livery conversion, structural
validation, verified backup, additive local import and recorded-file readback.
Windows 10/11 x64 and Python 3.11+ are supported. No desktop automation is used.

```powershell
python -m pip install .
fh6-livery-workbench --help
fh6-livery-workbench validate-project .\artwork.3so
fh6-livery-workbench validate-native .\export\C_livery
```

For a natural-language design workflow, setup and offscreen vehicle inspection,
give your Agent this link: [FH6 Livery Agent Workflow](https://github.com/lin522026298/FH6-Livery-Agent-Workflow).
An image-capable Agent, image generation and the user's own game resources are
needed for full authoring. Model subscriptions do not include these local tools.

## Own-export import sequence

1. Produce your own `.3so` and native `header` / `C_livery` export. Validate both.
2. Resolve the active save directory; use an explicit save root and a header from
   your own unpublished saved item as the identity seed. Keep game/editor windows
   under the user's control. This tool never starts them.
3. `stage` the authored export; inspect `validate-ready` and `dry-run-install`.
4. With the game closed and task authorization, create a complete verified backup,
   then `install ... --confirm INSTALL`. All cars/jobs use one `--project-root`.
5. `readback <installation-journal.json>` verifies the recorded installation again.
   The user applies, saves and publishes the design inside the game.

Use `--help` on each command for path arguments. Changes are additive; existing
save entries are never overwritten. A journal supports hash-checked `rollback`.
No account credentials, saves or character art are distributed here.

## Backup retention

The complete-save backup is project-wide, rolling **one**: create and verify the
new backup before import, read back the imported and all pre-existing files, then
prune earlier complete ZIPs. Keep manifests/logs/readback evidence. A failed import
or user-reported corruption puts retention on hold; preserve related backups
until the issue is resolved. Silence about damage counts as a normal prior import;
do not repeatedly ask the user to approve routine rotation. Source art, `.3so`
deliverables and actual game saves are not backup-cleanup targets.

## Development

```powershell
python -m pip install . pytest
python -m pytest
python scripts/check_distribution.py
python -m pip wheel . --no-deps --wheel-dir dist
```

The manifest lists every shipped package module. CI checks this list and the CLI
surface before building. Generic format parsing and encoding stay in the core.
The `src/` tree is the exact corresponding source for this package.

License: AGPL-3.0-only. See LICENSE and THIRD_PARTY.md for upstream attribution.
