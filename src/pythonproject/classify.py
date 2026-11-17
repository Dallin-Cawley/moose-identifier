"""Turn Jev's answers into a folder with code-side rules, and prepare images for upload.

The requests themselves are built by the identification strategies in identify.py.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage, ImageChops, ImageOps, ImageStat

from .questions import MOOSE, NO, YES


class Category(StrEnum):
    """Values are the folder paths inside an event folder."""

    MOOSE = "animal/moose"
    OTHER_ANIMAL = "animal/not_moose"
    # "Not a moose" calls in an event that also has moose photos: the usual way a moose is missed
    # (seen from behind for several frames). Set by identify.flag_for_review, never by decide().
    CHECK_NOT_MOOSE = "animal/not_moose_check"
    NO_ANIMAL = "no_animal"
    UNSURE = "unsure"


@dataclass(frozen=True)
class Result:
    path: Path
    category: Category
    animal: float   # P(an animal is present), from the `animal` Noul
    moose: float    # P(species is moose): average of the two species Choices (both option orders)
    species: str    # top species option of that average
    model: str
    input_tokens: int | None = None
    features: dict[str, float] = field(default_factory=dict)  # moose-anatomy Nouls
    smoothed: bool = False  # category changed by event smoothing (identify.smooth)


def has_moose_features(features: dict[str, float]) -> bool:
    """Observable moose anatomy: a shoulder hump plus pale long legs or a long overhanging muzzle."""
    if not features:
        return False
    return features["f_hump"] > 0.5 and (features["f_pale_legs"] > YES or features["f_long_muzzle"] > 0.5)


def read_answers(answers) -> tuple[float, dict[str, float], dict[str, float]]:
    """(P(animal), averaged species probabilities, feature probabilities) from a response's answers."""
    animal = answers["animal"].noul
    orders = [answers[k].probabilities for k in ("species", "species_rev") if k in answers]
    species = {opt: sum(o[opt] for o in orders) / len(orders) for opt in orders[0]}
    features = {k: a.noul for k, a in answers.items() if k.startswith("f_")}
    return animal, species, features


def _read(p: float) -> bool | None:
    """True = yes, False = no, None = unsettled."""
    if p > YES:
        return True
    if p < NO:
        return False
    return None


def _read_moose(moose: float, species: str) -> bool | None:
    """True = moose is the top species; False = moose unlikely (< NO); None = plausible but not top."""
    if species == MOOSE:
        return True
    if moose < NO:
        return False
    return None


def decide(animal: float, moose: float, species: str, moose_features: bool = False) -> Category:
    """Truth table over the judgments.

    animal: P(animal present). moose: P(species is moose). species: the top species option.
    moose_features: moose anatomy visible (has_moose_features). It never puts a photo in the
    moose folder; it only stops a "not a moose" verdict, sending the photo to `unsure` instead.
    """
    match _read(animal), _read_moose(moose, species):
        case None, _:
            return Category.UNSURE  # animal unsettled
        case True, True:
            return Category.MOOSE
        case True, False if moose_features:
            return Category.UNSURE  # Jev says another species, but moose anatomy is visible
        case True, False:
            return Category.OTHER_ANIMAL
        case True, None:
            return Category.UNSURE  # moose plausible but not the top species
        case False, _ if moose > YES:
            return Category.UNSURE  # contradiction: no animal, yet strongly a moose
        case False, _:
            return Category.NO_ANIMAL


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
