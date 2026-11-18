"""Turn a sorted output tree of symlinks into real files, and zip it up for sharing.

The sorter only ever creates symlinks, which are useless once the tree leaves this machine.
resolve_links() replaces each one with a copy of the original photo (same folder, same name),
so the tree can be copied, uploaded or zipped, and writes the photo's labels into the copy as XMP
keywords (see xmp.py). The originals are never moved or changed.

The top-level `unsure/` folder is the exception: it only ever holds symlinks (a record of photos with
an animal Jev couldn't identify, which are also copied into their event folder), so it is never
resolved, and the zip stores its entries as symlinks rather than photos.
"""

import os
import shutil
import stat
import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from .xmp import XmpError, embed_keywords, source_size


def is_copy_of(path: Path, original: Path) -> bool:
    """True if `path` is a regular file that resolve_links() copied from `original`.

    A copy keeps the original's modification time, and either its size or, once keywords are
    written into it, the original's size recorded in its XMP. That is enough to tell a resolved
    copy from an unrelated photo with the same name, without reading the original.
    """
    if path.is_symlink() or not path.is_file() or not original.is_file():
        return False
    a, b = path.stat(), original.stat()
    if int(a.st_mtime) != int(b.st_mtime):
        return False
    return a.st_size == b.st_size or source_size(path) == b.st_size


Keywords = Callable[[Path], Sequence[str] | None]

UNSURE_DIR = "unsure"   # top-level folder of symlinks only


def in_unsure(path: Path, out_dir: Path) -> bool:
    return path.relative_to(out_dir).parts[0] == UNSURE_DIR


def resolve_links(out_dir: Path, keywords: Keywords | None = None) -> tuple[int, list[Path]]:
    """Replace every symlink under out_dir, except those in unsure/, with a copy of the file it points to.

    keywords(original) gives the labels to write into the copy, or None if they aren't known; the
    copy is then left as a plain copy, with a warning. Returns (number of links resolved, broken
    links left in place). Already-resolved trees are a no-op, so this can be run again safely.
    """
    for stale in out_dir.rglob(".*.resolving"):   # left by an interrupted earlier run
        stale.unlink()
    resolved = 0
    broken: list[Path] = []
    links = sorted(p for p in out_dir.rglob("*") if p.is_symlink() and not in_unsure(p, out_dir))
    for i, link in enumerate(links, 1):
        target = link.resolve()
        if not target.is_file():
            broken.append(link)
            continue
        tmp = link.with_name(f".{link.name}.resolving")
        shutil.copy2(target, tmp)   # copy next to the link first, so an interruption leaves the link intact
        tag(tmp, target, keywords)
        os.replace(tmp, link)
        resolved += 1
        if i % 500 == 0 or i == len(links):
            print(f"resolved {i}/{len(links)} link(s)", file=sys.stderr)
    return resolved, broken


def tag(copy: Path, original: Path, keywords: Keywords | None) -> None:
    """Write the original's labels into the copy as XMP keywords, keeping the original's timestamps."""
    words = keywords(original) if keywords else None
    if not words:
        print(f"no labels known for {original.name}; copied without keywords", file=sys.stderr)
        return
    st = original.stat()
    try:
        embed_keywords(copy, words, st.st_size)
    except XmpError as e:
        print(f"keywords not written into {copy.name.removeprefix('.').removesuffix('.resolving')}: {e}",
              file=sys.stderr)
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))


def zip_dir(out_dir: Path, zip_path: Path) -> int:
    """Zip out_dir into zip_path, with the folder name as the top-level entry.

    Photos are already compressed, so they're stored as-is (much faster, barely bigger); text files
    are deflated. The links in unsure/ are stored as symlink entries (unzip on Linux and macOS
    recreates them). Any other symlink is refused rather than silently followed: resolve them first.
    Returns the number of entries written.
    """
    zip_path = zip_path.resolve()
    tmp = zip_path.with_name(zip_path.name + ".partial")
    files = sorted(p for p in out_dir.rglob("*")
                   if (p.is_file() or p.is_symlink()) and p.absolute() not in (zip_path, tmp))
    links = [p for p in files if p.is_symlink() and not in_unsure(p, out_dir)]
    if links:
        raise ValueError(f"{len(links)} symlink(s) in {out_dir} (e.g. {links[0]}); resolve them first")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for p in files:
            arcname = Path(out_dir.name) / p.relative_to(out_dir)
            if p.is_symlink():                       # unsure/: store the link itself, not the photo
                info = zipfile.ZipInfo(str(arcname))
                info.create_system = 3               # Unix, so the mode below is honoured
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, os.readlink(p))
                continue
            compress = zipfile.ZIP_DEFLATED if p.suffix.lower() in {".csv", ".jsonl", ".json", ".txt"} \
                else zipfile.ZIP_STORED
            zf.write(p, arcname, compress_type=compress)
    os.replace(tmp, zip_path)
    return len(files)
