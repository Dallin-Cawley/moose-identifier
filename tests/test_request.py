"""The exact request sent to Jev, and how responses and failures are handled."""

import asyncio
from datetime import datetime
from io import BytesIO

import httpx2
import pytest
from PIL import Image as PILImage
from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

from conftest import answer
from pythonproject.classify import MAX_SIDE, MIN_SIDE, is_infrared, prepare_image
from pythonproject.identify import SinglePhoto
from pythonproject.questions import ANIMALS, MODEL, OTHER_ANIMALS, SINGLE_QUESTIONS, questions_fingerprint


def run_classify(path):
    async def go():
        async with AsyncTypeSafeClient(headers={"TypeSafe-Danger-Zone-Images": "true"}) as client:
            return await SinglePhoto().identify(client, path)
    return asyncio.run(go())


def test_payload(fake_api, make_photo):
    photo = make_photo("a.jpg", datetime(2024, 1, 1))
    run_classify(photo)

    [sent] = fake_api.requests
    assert sent.body["model"] == MODEL == "jev-1.14.0"
    # tier 1: animal? tier 2: which species (moose?), in both option orders. tier 3: the others.
    assert {k: v["type"] for k, v in sent.body["questions"].items()} == {
        "animal": "noul", "species": "choice", "species_rev": "choice",
        "human": "noul", "dog": "noul", "cougar": "noul", "deer": "noul", "elk": "noul"}
    assert ANIMALS == ("human", "dog", "cougar", "deer", "elk", "moose")
    order = list(sent.body["questions"]["species"]["criteria"])
    assert "moose" in order and list(sent.body["questions"]["species_rev"]["criteria"]) == order[::-1]
    for qid, q in SINGLE_QUESTIONS.items():
        assert sent.body["questions"][qid]["instructions"] == q.instructions
        assert sent.body["questions"][qid]["criteria"] == q.criteria

    # the state is exactly one image reference, and its bytes are the prepared photo (banner cropped)
    ref = sent.body["state"]["photo"]
    assert set(sent.body["state"]) == {"photo"}
    part = sent.body["assets"][ref]["part"]
    assert sent.assets[part] == prepare_image(photo, crop_banner=True)


@pytest.mark.parametrize(("size", "expected"), [
    ((2688, 1512), (1568, 882)),    # the trail camera's size: shrink to the 1568 limit
    ((1000, 600), (1000, 600)),     # already within limits: unchanged
    ((32, 24), (373, 280)),         # below the 280 minimum: upscale
])
def test_prepare_image_size(tmp_path, size, expected):
    src = tmp_path / "a.jpg"
    PILImage.new("RGB", size).save(src, "JPEG")
    out = PILImage.open(BytesIO(prepare_image(src)))
    assert out.format == "JPEG" and out.size == expected
    assert MIN_SIDE <= min(out.size) and max(out.size) <= MAX_SIDE


def test_prepare_image_strips_metadata_and_rotates(tmp_path):
    src = tmp_path / "a.jpg"
    exif = PILImage.Exif()
    exif[0x0112] = 6                                   # orientation: rotate 90° on display
    exif[0x8825] = {1: "N", 2: (45.0, 0.0, 0.0)}       # GPS IFD
    exif.get_ifd(0x8769)[0x9003] = "2024:10:08 09:03:13"
    PILImage.new("RGB", (600, 400)).save(src, "JPEG", exif=exif)
    out = PILImage.open(BytesIO(prepare_image(src)))
    assert out.size == (400, 600)                      # pixels rotated upright
    assert len(out.getexif()) == 0                     # no EXIF at all, so no GPS


def test_prepare_image_crops_banner(tmp_path):
    src = tmp_path / "a.jpg"
    PILImage.new("RGB", (2688, 1512)).save(src, "JPEG")
    out = PILImage.open(BytesIO(prepare_image(src, crop_banner=True)))
    assert out.size == (1568, 833)  # 1428 rows kept of 1512, then scaled to 1568 wide


def test_is_infrared(tmp_path):
    assert is_infrared(PILImage.new("RGB", (100, 50), (90, 90, 90)))
    assert not is_infrared(PILImage.new("RGB", (100, 50), (120, 90, 40)))


def test_questions_refer_to_state_field():
    for q in SINGLE_QUESTIONS.values():
        assert "`photo`" in q.instructions


def test_result_fields(fake_api, make_photo):
    fake_api.respond = lambda req: answer(tokens=1234, animal=0.96, moose=0.91, dog=0.75)
    r = run_classify(make_photo("a.jpg", datetime(2024, 1, 1)))
    assert (r.animals, r.model, r.input_tokens) == (("dog", "moose"), "jev-1.14.0", 1234)
    assert r.moose == pytest.approx(0.91) and r.species_top == "moose"
    assert set(r.probabilities) == {"animal", "species", *OTHER_ANIMALS}


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_api_error_raises_not_answers(fake_api, make_photo, status):
    fake_api.respond = lambda req: httpx2.Response(status, json={"error": {"message": "boom"}})
    with pytest.raises(TypeSafeError):
        run_classify(make_photo("a.jpg", datetime(2024, 1, 1)))


def test_fingerprint_tracks_wording():
    from typesafe_sdk import Noul
    before = questions_fingerprint(SINGLE_QUESTIONS)
    assert before == questions_fingerprint(SINGLE_QUESTIONS)  # stable
    changed = dict(SINGLE_QUESTIONS, animal=Noul(instructions="Is there an animal in `photo`?"))
    assert questions_fingerprint(changed) != before
