"""Turn a sorted output tree of symlinks into real files, and zip it up for sharing.

The sorter only ever creates symlinks, which are useless once the tree leaves this machine.
resolve_links() replaces each one with a copy of the original photo (same folder, same name),
so the tree can be copied, uploaded or zipped. The originals are never moved or changed.
"""

import os
import shutil
import sys
import zipfile
from pathlib import Path


def is_copy_of(path: Path, original: Path) -> bool:
    """True if `path` is a regular file that resolve_links() copied from `original`.

    shutil.copy2 keeps the size and modification time, which is enough to tell a resolved copy
    from an unrelated photo with the same name, without reading either file.
    """
    if path.is_symlink() or not path.is_file() or not original.is_file():
        return False
    a, b = path.stat(), original.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def resolve_links(out_dir: Path) -> tuple[int, list[Path]]:
    """Replace every symlink under out_dir with a copy of the file it points to.

    Returns (number of links resolved, broken links left in place). Already-resolved trees are a
    no-op, so this can be run again safely.
    """
    for stale in out_dir.rglob(".*.resolving"):   # left by an interrupted earlier run
        stale.unlink()
    resolved = 0
    broken: list[Path] = []
    links = sorted(p for p in out_dir.rglob("*") if p.is_symlink())
    for i, link in enumerate(links, 1):
        target = link.resolve()
        if not target.is_file():
            broken.append(link)
            continue
        tmp = link.with_name(f".{link.name}.resolving")
        shutil.copy2(target, tmp)   # copy next to the link first, so an interruption leaves the link intact
        os.replace(tmp, link)
        resolved += 1
        if i % 500 == 0 or i == len(links):
            print(f"resolved {i}/{len(links)} link(s)", file=sys.stderr)
    return resolved, broken


def zip_dir(out_dir: Path, zip_path: Path) -> int:
    """Zip out_dir into zip_path, with the folder name as the top-level entry.

    Photos are already compressed, so they're stored as-is (much faster, barely bigger); text files
    are deflated. Symlinks are refused rather than silently followed: resolve them first.
    Returns the number of files written.
    """
    zip_path = zip_path.resolve()
    tmp = zip_path.with_name(zip_path.name + ".partial")
    files = sorted(p for p in out_dir.rglob("*")
                   if (p.is_file() or p.is_symlink()) and p.absolute() not in (zip_path, tmp))
    links = [p for p in files if p.is_symlink()]
    if links:
        raise ValueError(f"{len(links)} symlink(s) in {out_dir} (e.g. {links[0]}); resolve them first")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for p in files:
            arcname = Path(out_dir.name) / p.relative_to(out_dir)
            compress = zipfile.ZIP_DEFLATED if p.suffix.lower() in {".csv", ".jsonl", ".json", ".txt"} \
                else zipfile.ZIP_STORED
            zf.write(p, arcname, compress_type=compress)
    os.replace(tmp, zip_path)
    return len(files)
