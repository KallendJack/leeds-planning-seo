"""Overlay output_staging/ onto the live output/ WITHOUT replacing the
directory (a bind-mounted dir must keep its inode). Files are copied over the
top first, then anything stale is pruned, so the site is never empty."""
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
src, dst = ROOT / "output_staging", ROOT / "output"

if not src.is_dir():
    raise SystemExit("no output_staging/ — run build_staging.py first")

copied = 0
for f in src.rglob("*"):
    if f.is_file():
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1

removed = 0
for f in sorted(dst.rglob("*"), reverse=True):
    if f.is_file() and not (src / f.relative_to(dst)).exists():
        f.unlink()
        removed += 1
    elif f.is_dir():
        try:
            f.rmdir()
        except OSError:
            pass

print(f"copied {copied} files, pruned {removed} stale files")
print(f"live html pages: {sum(1 for _ in dst.rglob('*.html'))}")
shutil.rmtree(src)
print("staging removed")
