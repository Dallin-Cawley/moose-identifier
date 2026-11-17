"""End-to-end CLI runs against the fake API."""

import csv
import json
import sys
from datetime import datetime, timedelta

import httpx2
import pytest

import pythonproject
from conftest import answer
from pythonproject.classify import prepare_image

T0 = datetime(2024, 10, 30, 16, 28, 4)

def by_photo(table: dict[bytes, tuple[float, float]]):
    def respond(req):
        [data] = req.assets.values()
        if data not in table:
            return httpx2.Response(500, json={"error": {"message": "unknown photo"}})
        return answer(*table[data])
    return respond


def run_cli(*args) -> int:
    argv = sys.argv
    sys.argv = ["moose-sort", *map(str, args)]
    try:
        pythonproject.main()
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = argv
    return 0


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "api_key"
    p.write_text("test-key\n")
    return p


@pytest.fixture
def scene(make_photo, tmp_path, fake_api):
    """5 photos across two static windows; one photo has no answer configured (API error)."""
    spec = {  # name: (minutes after T0, animal, moose)
        "moose1.jpg": (0, 0.95, 0.92),
        "deer.jpg": (3, 0.93, 0.04),
        "empty.jpg": (9, 0.02, 0.03),
        "blurry.jpg": (12, 0.55, 0.40),
        "moose2.jpg": (14, 0.97, 0.95),
    }
    table = {}
    for i, (name, (m, a, mo)) in enumerate(spec.items()):
        p = make_photo(name, T0 + timedelta(minutes=m), color=(i * 40, 100, 60))  # distinct bytes
        table[prepare_image(p, crop_banner=True)] = (a, mo)
    fake_api.respond = by_photo(table)
    return tmp_path / "photos"


def tree(out):
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_symlink())


def test_static_layout(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 0
    assert tree(out) == [
        "event_0000_2024-10-30_162804/animal/moose/moose1.jpg",
        "event_0000_2024-10-30_162804/animal/not_moose_check/deer.jpg",  # same event as a moose
        "event_0000_2024-10-30_162804/no_animal/empty.jpg",
        "event_0001_2024-10-30_163804/animal/moose/moose2.jpg",
        "event_0001_2024-10-30_163804/unsure/blurry.jpg",
    ]
    link = out / "event_0000_2024-10-30_162804/animal/moose/moose1.jpg"
    assert link.resolve() == (scene / "moose1.jpg").resolve()


def test_rolling_layout(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "-e", "rolling", "--api-key-file", key_file) == 0
    # rolling: minute 12 is outside [0, 10), so a new event starts at 16:40:04
    assert {p.split("/")[0] for p in tree(out)} == {
        "event_0000_2024-10-30_162804", "event_0001_2024-10-30_164004"}


def test_events_csv_and_results_jsonl(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    rows = list(csv.DictReader(open(out / "events.csv")))
    assert [r["folder"] for r in rows] == ["event_0000_2024-10-30_162804", "event_0001_2024-10-30_163804"]
    assert {k: rows[0][k] for k in ("moose", "not_moose", "not_moose_check", "no_animal", "unsure", "errors")} == {
        "moose": "1", "not_moose": "0", "not_moose_check": "1", "no_animal": "1", "unsure": "0", "errors": "0"}

    lines = [json.loads(l) for l in open(out / "results.jsonl")]
    assert len(lines) == 5
    assert set(lines[0]) >= {"path", "event", "event_folder", "event_start", "strategy",
                             "category", "animal", "moose", "species", "model", "input_tokens"}


def test_api_failure_is_not_sorted_and_exits_1(scene, tmp_path, key_file, make_photo):
    make_photo("unknown.jpg", T0 + timedelta(minutes=1), folder=scene)  # fake API returns 500
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 1
    assert not any("unknown" in p for p in tree(out))
    rows = list(csv.DictReader(open(out / "events.csv")))
    assert rows[0]["errors"] == "1"


def test_missing_timestamp_is_error(scene, tmp_path, key_file, make_photo):
    make_photo("nodate.jpg", None, folder=scene)
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 1
    assert not any("nodate" in p for p in tree(out))


def test_rerun_is_idempotent(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    first = tree(out)
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert tree(out) == first


def test_name_collision_gets_suffix(make_photo, tmp_path, key_file):
    a = make_photo("same.jpg", T0, folder=tmp_path / "a")
    b = make_photo("same.jpg", T0 + timedelta(minutes=1), folder=tmp_path / "b", color=(1, 2, 3))
    out = tmp_path / "out"
    assert run_cli(a, b, "-o", out, "--api-key-file", key_file) == 0
    links = [p for p in out.rglob("*") if p.is_symlink()]
    assert len(links) == 2 and {l.resolve() for l in links} == {a.resolve(), b.resolve()}


def test_output_dir_inside_input_is_not_rescanned(scene, key_file, fake_api):
    out = scene / "sorted"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    n = len(fake_api.requests)
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert len(fake_api.requests) == 2 * n  # same photos again, not the links


def test_dry_run_writes_nothing(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "-n", "--api-key-file", key_file) == 0
    assert not out.exists()


def test_api_key_from_file_is_sent(scene, tmp_path, fake_api, monkeypatch):
    # bypass the fixture's key override to see what the CLI passes
    import typesafe_sdk
    seen = {}
    patched = typesafe_sdk.AsyncTypeSafeClient.__init__
    def spy(self, **kw):
        seen["key"] = kw.get("api_key")
        seen["headers"] = kw.get("headers")
        patched(self, **kw)
    monkeypatch.setattr(typesafe_sdk.AsyncTypeSafeClient, "__init__", spy)
    key = tmp_path / "k"
    key.write_text("  secret-from-file \n")
    run_cli(scene, "-o", tmp_path / "out", "--api-key-file", key)
    assert seen == {"key": "secret-from-file", "headers": {"TypeSafe-Danger-Zone-Images": "true"}}


def test_no_api_key_is_usage_error(scene, tmp_path):
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key-file", tmp_path / "missing") == 2


# --- identification strategy, cost estimate and smoothing ---------------------------------------

def test_estimate_prints_cost_and_sends_nothing(scene, tmp_path, fake_api, capsys):
    rc = run_cli(scene, "-o", tmp_path / "out", "--estimate", "--api-key-file", tmp_path / "missing")
    assert rc == 0 and fake_api.requests == []          # no key needed, nothing sent
    err = capsys.readouterr().err
    assert "5 image(s) to send, estimated $" in err
    assert not (tmp_path / "out").exists()


@pytest.fixture
def walkaway(make_photo, tmp_path, fake_api):
    """Moose, then the same moose from behind (Jev: deer, P(moose) 0.2), then moose again."""
    spec = {"a.jpg": (0, 0.97, 0.9), "b.jpg": (1, 0.97, 0.2), "c.jpg": (2, 0.97, 0.9)}
    table = {}
    for i, (name, (m, a, mo)) in enumerate(spec.items()):
        p = make_photo(name, T0 + timedelta(minutes=m), folder=tmp_path / "walk", color=(i * 70, 50, 50))
        table[prepare_image(p, crop_banner=True)] = (a, mo)
    fake_api.respond = by_photo(table)
    return tmp_path / "walk"


def test_smoothing_promotes_frame_between_moose(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(walkaway, "-o", out, "--api-key-file", key_file) == 0
    assert tree(out) == [f"event_0000_2024-10-30_162804/animal/moose/{n}.jpg" for n in "abc"]
    lines = {json.loads(l)["path"].rsplit("/", 1)[1]: json.loads(l) for l in open(out / "results.jsonl")}
    assert lines["b.jpg"]["smoothed"] is True and lines["a.jpg"]["smoothed"] is False


def test_no_smooth_flag(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(walkaway, "-o", out, "--no-smooth", "--api-key-file", key_file)
    # not smoothed, so it stays "not a moose", but in the check folder: its event has moose photos
    assert "event_0000_2024-10-30_162804/animal/not_moose_check/b.jpg" in tree(out)


def test_deer_in_an_event_without_moose_stays_in_not_moose(make_photo, tmp_path, key_file, fake_api):
    fake_api.respond = lambda req: answer(0.96, 0.03)
    make_photo("deer1.jpg", T0, folder=tmp_path / "d")
    make_photo("deer2.jpg", T0 + timedelta(minutes=1), folder=tmp_path / "d", color=(1, 2, 3))
    out = tmp_path / "out"
    assert run_cli(tmp_path / "d", "-o", out, "--api-key-file", key_file) == 0
    assert tree(out) == [f"event_0000_2024-10-30_162804/animal/not_moose/deer{i}.jpg" for i in (1, 2)]


def test_placeholder_key_counts_as_no_key(scene, tmp_path, monkeypatch):
    key = tmp_path / "api_key"
    key.write_text("<put your TypeSafe API key here: create one at https://console.typesafe.ai/>\n")
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key-file", key) == 2   # usage error, nothing sent
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    assert pythonproject.load_api_key(key) == "from-env"
