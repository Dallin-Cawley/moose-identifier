"""Prepare photos for upload to Jev.

The requests are built in identify.py; answers become labels in labels.py.
"""

from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage, ImageChops, ImageOps, ImageStat

# API image limits: each side 280..1568 px. Larger or smaller images are rejected, not resized.
MAX_SIDE = 1568
MIN_SIDE = 280


# This camera (Stealth Cam GMAX32V, 2688x1512) prints a date/time/temperature banner in the
# bottom 84 rows. It is camera overlay, not scene, so it can be cropped off.
BANNER_FRACTION = 84 / 1512
# Night photos are infrared: the three colour channels are (almost) identical.
IR_MAX_CHANNEL_SPREAD = 2.0


def is_infrared(img: PILImage.Image) -> bool:
    r, g, b = img.convert("RGB").resize((64, 36)).split()
    spread = (ImageStat.Stat(ImageChops.difference(r, g)).mean[0]
              + ImageStat.Stat(ImageChops.difference(g, b)).mean[0])
    return spread < IR_MAX_CHANNEL_SPREAD


def prepare_image(path: Path, *, crop_banner: bool = False, enhance_ir: bool = False,
                  box: tuple[float, float, float, float] | None = None) -> bytes:
    """Upright, within the API's size limits, baseline JPEG, and no metadata (so no GPS is uploaded).

    crop_banner: drop the camera's bottom banner. enhance_ir: stretch contrast on infrared photos.
    box: (left, top, right, bottom) fractions of the (banner-cropped) image to crop to, e.g. a zoom.
    """
    with PILImage.open(path) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
    if crop_banner:
        img = img.crop((0, 0, img.width, round(img.height * (1 - BANNER_FRACTION))))
    if box:
        l, t, r, b = box
        img = img.crop((round(l * img.width), round(t * img.height), round(r * img.width), round(b * img.height)))
    if enhance_ir and is_infrared(img):
        img = ImageOps.autocontrast(img, cutoff=1)
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    if min(img.size) < MIN_SIDE:
        scale = MIN_SIDE / min(img.size)
        img = img.resize((min(MAX_SIDE, round(img.width * scale)), min(MAX_SIDE, round(img.height * scale))))
    out = BytesIO()
    img.save(out, "JPEG", quality=90)  # no exif= argument: metadata is dropped
    return out.getvalue()
