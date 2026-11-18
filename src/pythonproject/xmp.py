"""Write a photo's labels into a copy of it as XMP keywords, without re-encoding the image.

The labels go into XMP `dc:subject`, the standard keyword field read by Lightroom, digiKam,
darktable, Bridge, Windows Explorer ("Tags"), macOS and most photo apps. The XMP packet is inserted
as its own segment (a JPEG APP1 segment, a PNG iTXt chunk); every other byte of the file, including
the camera's EXIF, is kept as it was, so the image itself is unchanged.

Only exported copies get keywords (export.resolve_links); originals are never written to.
A copy also records the size of the original it came from (`mooseSort:sourceSize`), so a re-run can
still recognise it as a copy of that original (export.is_copy_of).
"""

import re
import struct
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

_JPEG_XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_XMP_KEY = b"XML:com.adobe.xmp"
NS = "https://moose-sort.invalid/ns/1.0/"   # our own namespace; marks packets we wrote
_SOURCE_SIZE = re.compile(rb'mooseSort:sourceSize="(\d+)"')


class XmpError(ValueError):
    """The file can't take our keywords (unsupported format, or it already has foreign XMP)."""


def packet(keywords, source_size: int) -> bytes:
    items = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in keywords)
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        f'xmlns:mooseSort="{NS}" mooseSort:sourceSize="{source_size}">'
        f"<dc:subject><rdf:Bag>{items}</rdf:Bag></dc:subject>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode()


def _is_ours(xmp: bytes) -> bool:
    return NS.encode() in xmp


def embed_jpeg(data: bytes, xmp: bytes) -> bytes:
    """Insert an XMP APP1 segment after the leading APP0/APP1 (JFIF, EXIF) segments.

    An XMP segment from an earlier export of ours is replaced; anyone else's is left alone (XmpError).
    """
    if data[:2] != b"\xff\xd8":
        raise XmpError("not a JPEG")
    body = _JPEG_XMP_ID + xmp
    if len(body) + 2 > 0xFFFF:
        raise XmpError("too many keywords for one XMP segment")
    segment = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    out, pos, inserted = [data[:2]], 2, False
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA or 0xD0 <= marker <= 0xD9:   # start of scan / markers without a length
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        end = pos + 2 + length
        if marker == 0xE1 and data[pos + 4:end].startswith(_JPEG_XMP_ID):
            if not _is_ours(data[pos + 4:end]):
                raise XmpError("already has XMP metadata from another program; left as it was")
            pos = end                                   # ours from an earlier export: drop, re-add below
            continue
        if not inserted and marker not in (0xE0, 0xE1):
            out.append(segment)
            inserted = True
        out.append(data[pos:end])
        pos = end
    if not inserted:
        out.append(segment)
    out.append(data[pos:])
    return b"".join(out)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def embed_png(data: bytes, xmp: bytes) -> bytes:
    """Insert (or replace our earlier) iTXt XMP chunk right after IHDR."""
    if not data.startswith(_PNG_SIG):
        raise XmpError("not a PNG")
    pos, chunks = len(_PNG_SIG), []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        payload = data[pos + 8:pos + 8 + length]
        if kind == b"iTXt" and payload.startswith(_PNG_XMP_KEY + b"\x00"):
            if not _is_ours(payload):
                raise XmpError("already has XMP metadata from another program; left as it was")
        else:
            chunks.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            break
    itxt = _png_chunk(b"iTXt", _PNG_XMP_KEY + b"\x00\x00\x00" + b"\x00" + b"\x00" + xmp)
    return _PNG_SIG + chunks[0] + itxt + b"".join(chunks[1:])


def embed_keywords(path: Path, keywords, source_size: int) -> None:
    """Write keywords into the file at `path` (a copy, never an original). Raises XmpError.

    The format comes from the file's first bytes, not its name (copies are written to a temp name).
    """
    data = path.read_bytes()
    xmp = packet(keywords, source_size)
    if data.startswith(b"\xff\xd8"):
        new = embed_jpeg(data, xmp)
    elif data.startswith(_PNG_SIG):
        new = embed_png(data, xmp)
    else:
        raise XmpError("can't write keywords into this file type (only JPEG and PNG)")
    path.write_bytes(new)


def source_size(path: Path) -> int | None:
    """The original's size recorded in a copy we wrote keywords into, else None."""
    with open(path, "rb") as f:
        head = f.read(256 * 1024)
    m = _SOURCE_SIZE.search(head)
    return int(m.group(1)) if m else None
