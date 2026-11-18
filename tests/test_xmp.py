"""Writing labels into exported copies as XMP keywords, without touching the image data."""

from io import BytesIO

import pytest
from PIL import Image

from pythonproject.xmp import XmpError, embed_jpeg, embed_keywords, embed_png, packet, source_size
from test_cli import keywords_in


def jpeg(exif: bool = True) -> bytes:
    out = BytesIO()
    ex = Image.Exif()
    if exif:
        ex.get_ifd(0x8769)[0x9003] = "2024:10:30 16:28:04"
    Image.new("RGB", (40, 30), (120, 80, 40)).save(out, "JPEG", exif=ex)
    return out.getvalue()


def png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(out, "PNG")
    return out.getvalue()


def pixels(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as im:
        return im.tobytes()


def test_jpeg_keywords_without_reencoding():
    data = jpeg()
    tagged = embed_jpeg(data, packet(["moose", "deer"], len(data)))
    assert keywords_in(tagged) == ["moose", "deer"]
    assert pixels(tagged) == pixels(data)
    assert len(tagged) - len(data) == len(packet(["moose", "deer"], len(data))) + 4 + 29  # one segment added
    with Image.open(BytesIO(tagged)) as im:
        assert im.getexif().get_ifd(0x8769)[0x9003] == "2024:10:30 16:28:04"        # EXIF kept


def test_xmp_goes_after_exif():
    data = jpeg()
    tagged = embed_jpeg(data, packet(["moose"], 1))
    assert tagged.index(b"Exif\x00\x00") < tagged.index(b"http://ns.adobe.com/xap/1.0/")


def test_jpeg_without_exif():
    data = jpeg(exif=False)
    assert keywords_in(embed_jpeg(data, packet(["elk"], 1))) == ["elk"]


def test_our_earlier_keywords_are_replaced_not_duplicated():
    data = jpeg()
    once = embed_jpeg(data, packet(["deer"], 1))
    twice = embed_jpeg(once, packet(["moose"], 1))
    assert keywords_in(twice) == ["moose"] and twice.count(b"http://ns.adobe.com/xap/1.0/") == 1


def test_foreign_xmp_is_left_alone():
    data = jpeg()
    foreign = embed_jpeg(data, packet(["x"], 1)).replace(b"moose-sort.invalid", b"someone-else.org/")
    with pytest.raises(XmpError, match="another program"):
        embed_jpeg(foreign, packet(["moose"], 1))


def test_png_keywords():
    data = png()
    tagged = embed_png(data, packet(["cougar"], len(data)))
    assert keywords_in(tagged) == ["cougar"] and pixels(tagged) == pixels(data)
    assert keywords_in(embed_png(tagged, packet(["dog"], 1))) == ["dog"]


def test_keywords_are_escaped():
    assert keywords_in(embed_jpeg(jpeg(), packet(["a<b&c"], 1))) == ["a<b&c"]


def test_embed_keywords_detects_format_by_content(tmp_path):
    p = tmp_path / ".STC_0001.JPG.resolving"     # the temp name used while exporting
    p.write_bytes(jpeg())
    embed_keywords(p, ["moose"], 12345)
    assert keywords_in(p.read_bytes()) == ["moose"] and source_size(p) == 12345
    other = tmp_path / "x.gif"
    other.write_bytes(b"GIF89a....")
    with pytest.raises(XmpError):
        embed_keywords(other, ["moose"], 1)


def test_source_size_absent(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(jpeg())
    assert source_size(p) is None
