"""Shared fixtures. Every test runs against a fake TypeSafe API; real network calls fail the test."""

import email.parser
import email.policy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx2
import pytest
import typesafe_sdk
from compression import zstd
from PIL import Image

PROJECT = Path(__file__).resolve().parent.parent
IMAGES = PROJECT / "src" / "images"


@dataclass
class SentRequest:
    headers: httpx2.Headers
    body: dict          # the JSON "request" part
    assets: dict[str, bytes]  # multipart file parts by name


def parse_request(req: httpx2.Request, raw: bytes) -> SentRequest:
    if req.headers.get("content-encoding") == "zstd":
        raw = zstd.decompress(raw)
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + raw
    )
    body, assets = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True)
        if name == "request":
            body = json.loads(data)
        else:
            assets[name] = data
    return SentRequest(req.headers, body, assets)


def answer(tokens: int = 1000, animal: float = 0.02, moose: float = 0.02, **others: float) -> httpx2.Response:
    """Fake Jev response for the production questions.

    animal: the `animal` Noul. moose: P(moose) in both species Choices; the rest of the Choice goes
    mostly to deer. others: the human/dog/cougar/deer/elk Nouls (default 0.02, clearly absent).
    """
    from pythonproject.questions import OTHER_ANIMALS, SPECIES
    unknown = set(others) - set(OTHER_ANIMALS)
    assert not unknown, f"not a question: {unknown}"
    rest = 1 - moose
    probs = {"moose": moose, "deer": rest * 0.8, "elk": rest * 0.05, "cougar": rest * 0.05,
             "dog": rest * 0.05, "small_animal": rest * 0.05}
    assert set(probs) == set(SPECIES.criteria)
    choice = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs, "confidence": 0.5}
    return httpx2.Response(200, json={
        "model": "jev-1.14.0", "usage": {"input_tokens": tokens},
        "answers": {
            "animal": {"type": "noul", "noul": animal},
            "species": choice,
            "species_rev": {**choice, "probabilities": dict(reversed(list(probs.items())))},
            **{a: {"type": "noul", "noul": others.get(a, 0.02)} for a in OTHER_ANIMALS},
        },
    })


def make_labels(path="a.jpg", animal: float = 0.02, moose: float = 0.02, **others: float):
    """Labels as the production code reads them, with the same species split as answer()."""
    from pathlib import Path as _P
    from pythonproject.labels import Labels
    from pythonproject.questions import OTHER_ANIMALS
    rest = 1 - moose
    species = {"moose": moose, "deer": rest * 0.8, "elk": rest * 0.05, "cougar": rest * 0.05,
               "dog": rest * 0.05, "small_animal": rest * 0.05}
    probs = {"animal": animal, "species": species, **{a: others.get(a, 0.02) for a in OTHER_ANIMALS}}
    return Labels(_P(path), probs, "jev-1.14.0", 100)


@dataclass
class FakeAPI:
    """Set `respond` to decide each answer; every request is recorded in `requests`."""
    respond: Callable[[SentRequest], httpx2.Response] = lambda req: answer(animal=0.9, deer=0.9)
    requests: list[SentRequest] = field(default_factory=list)

    async def handle(self, req: httpx2.Request) -> httpx2.Response:
        sent = parse_request(req, await req.aread())
        self.requests.append(sent)
        return self.respond(sent)


@pytest.fixture(autouse=True)
def fake_api(monkeypatch) -> FakeAPI:
    """Autouse: route every client to the fake API, so no test can reach the real one."""
    api = FakeAPI()
    original = typesafe_sdk.AsyncTypeSafeClient.__init__

    def patched(self, **kwargs):
        kwargs["transport"] = httpx2.MockTransport(api.handle)
        kwargs["api_key"] = "test-key"
        kwargs["retry"] = typesafe_sdk.RetryPolicy(max_retries=0)
        original(self, **kwargs)

    def no_sync_client(*a, **k):
        raise AssertionError("tests must not create a real TypeSafeClient")

    monkeypatch.setattr(typesafe_sdk.AsyncTypeSafeClient, "__init__", patched)
    monkeypatch.setattr(typesafe_sdk.TypeSafeClient, "__init__", no_sync_client)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return api


@pytest.fixture
def make_photo(tmp_path) -> Callable[..., Path]:
    """Create a small JPEG with an EXIF capture time."""
    def make(name: str, taken: datetime | None, folder: Path | None = None,
             color: tuple[int, int, int] = (90, 120, 60)) -> Path:
        folder = folder or tmp_path / "photos"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        exif = Image.Exif()
        if taken:
            exif.get_ifd(0x8769)[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
        Image.new("RGB", (32, 24), color).save(path, "JPEG", exif=exif)
        return path
    return make


requires_images = pytest.mark.skipif(not IMAGES.is_dir(), reason="src/images not present")
