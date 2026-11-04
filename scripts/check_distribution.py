"""Check the shipped module manifest and headless command contract."""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from fh6_livery_workbench.cli import _parser

def main():
    m=json.loads((ROOT/"distribution.json").read_text())
    package=ROOT/"src/fh6_livery_workbench"
    actual={p.name for p in package.iterdir() if p.is_file()}
    if actual != set(m["modules"]):raise SystemExit("Package module manifest mismatch")
    sub=next(a for a in _parser()._actions if hasattr(a,"choices") and isinstance(a.choices,dict))
    if set(sub.choices) != set(m["commands"]):raise SystemExit("CLI contract mismatch")
    for name,digest in m["files"].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest:
            raise SystemExit("Distribution file hash mismatch: "+name)
    print(json.dumps(dict(status="DISTRIBUTION_VERIFIED",modules=len(actual),commands=len(sub.choices))))

if __name__ == "__main__":main()
