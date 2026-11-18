"""End-to-end CLI runs against the fake API."""

import csv
import json
import sys
import zipfile
from datetime import datetime, timedelta
from io import BytesIO

import httpx2
import pytest
from PIL import Image as PILImage

import pythonproject
from conftest import answer
from pythonproject.classify import prepare_image
from pythonproject.xmp import source_size

T0 = datetime(2024, 10, 30, 16, 28, 4)


def by_photo(table: dict[bytes, dict[str, float]]):
    def respond(req):
        [data] = req.assets.values()
        if data not in table:
            return httpx2.Response(500, json={"error": {"message": "unknown photo"}})
        return answer(**table[data])
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


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run every CLI test from an empty directory, so a developer's ./moose-sort.hcl can't leak in."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "api_key"
    p.write_text("test-key\n")
    return p


def make_scene(make_photo, fake_api, folder, spec):
    """spec: name -> (minutes after T0, answer() arguments). Distinct colours = distinct bytes."""
    table = {}
    for i, (name, (m, probs)) in enumerate(spec.items()):
        p = make_photo(name, T0 + timedelta(minutes=m), folder=folder, color=(i * 40 % 256, 100, 60))
        table[prepare_image(p, crop_banner=True)] = probs
    fake_api.respond = by_photo(table)
    return folder


# Three events: a moose with a deer, 11 minutes without an animal, a moose alone, then a person
# with a dog. The empty frame and the blurry one (Jev unsure about an elk) are dropped.
SCENE = {
    "moose1.jpg": (0, {"animal": 0.95, "moose": 0.95}),
    "deer.jpg": (3, {"animal": 0.93, "moose": 0.05, "deer": 0.93}),
    "empty.jpg": (9, {}),
    "moose2.jpg": (14, {"animal": 0.97, "moose": 0.97}),
    "blurry.jpg": (16, {"animal": 0.5, "elk": 0.5}),
    "walker.jpg": (40, {"animal": 0.9, "human": 0.9, "dog": 0.85}),
}
MIXED, MOOSE, OTHER = "2024-10-30_162804_moose-deer", "2024-10-30_164204_moose", "2024-10-30_170804_human-dog"
LINKED = 4                                               # photos placed in events
REPORTS = ["events.csv", "labels.jsonl", "results.jsonl"]


@pytest.fixture
def scene(make_photo, tmp_path, fake_api):
    return make_scene(make_photo, fake_api, tmp_path / "photos", SCENE)


def tree(out):
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_symlink())


def files_in(out):
    """Regular files (not symlinks) under out, relative paths."""
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and not p.is_symlink())


def keywords_in(path):
    """dc:subject keywords of a file, parsed from its XMP packet as a photo app would."""
    import re
    import xml.etree.ElementTree as ET
    data = path.read_bytes() if hasattr(path, "read_bytes") else path
    m = re.search(rb"<x:xmpmeta.*?</x:xmpmeta>", data, re.S)
    if not m:
        return []
    root = ET.fromstring(m.group(0))
    rdf, dc = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}", "{http://purl.org/dc/elements/1.1/}"
    return [li.text for li in root.iter(f"{rdf}li") if li in list(root.iter(f"{dc}subject"))[0].iter()]


# --- events and labels -----------------------------------------------------------------------------

def test_layout(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 0
    assert tree(out) == [f"{MIXED}/deer.jpg", f"{MIXED}/moose1.jpg", f"{MOOSE}/moose2.jpg",
                         f"{OTHER}/walker.jpg"]
    assert (out / MIXED / "moose1.jpg").resolve() == (scene / "moose1.jpg").resolve()
    assert sorted(files_in(out)) == REPORTS


def test_events_csv(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    rows = list(csv.DictReader(open(out / "events.csv")))
    assert [(r["folder"], r["name"], r["type"], r["photos"]) for r in rows] == [
        (MIXED, "moose-deer", "mixed", "2"), (MOOSE, "moose", "moose", "1"), (OTHER, "human-dog", "other", "1")]
    assert {a: rows[0][a] for a in ("human", "dog", "cougar", "deer", "elk", "moose")} == {
        "human": "0", "dog": "0", "cougar": "0", "deer": "1", "elk": "0", "moose": "1"}
    assert (rows[0]["start"], rows[0]["end"]) == ("2024-10-30T16:28:04", "2024-10-30T16:31:04")


def test_results_jsonl_lists_every_photo(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    lines = {json.loads(l)["path"].rsplit("/", 1)[1]: json.loads(l) for l in open(out / "results.jsonl")}
    assert len(lines) == 6
    assert lines["walker.jpg"]["labels"] == ["human", "dog"] and lines["walker.jpg"]["event"] == "human-dog"
    assert lines["empty.jpg"]["labels"] == ["no_animal"] and lines["empty.jpg"]["event_folder"] is None
    assert lines["blurry.jpg"]["labels"] == ["no_animal"] and lines["blurry.jpg"]["probabilities"]["elk"] == 0.5
    assert set(lines["moose1.jpg"]) >= {"taken", "probabilities", "smoothed", "cached", "model", "input_tokens"}


def test_api_failure_is_not_sorted_and_exits_1(scene, tmp_path, key_file, make_photo):
    make_photo("unknown.jpg", T0 + timedelta(minutes=1), folder=scene)  # fake API returns 500
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 1
    assert not any("unknown" in p for p in tree(out))
    assert len(tree(out)) == LINKED


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


def test_name_collision_gets_suffix(make_photo, tmp_path, key_file, fake_api):
    fake_api.respond = lambda req: answer(animal=0.9, moose=0.9)
    a = make_photo("same.jpg", T0, folder=tmp_path / "a")
    b = make_photo("same.jpg", T0 + timedelta(minutes=1), folder=tmp_path / "b", color=(1, 2, 3))
    out = tmp_path / "out"
    assert run_cli(a, b, "-o", out, "--api-key-file", key_file) == 0
    links = [p for p in out.rglob("*") if p.is_symlink()]
    assert len(links) == 2 and {l.resolve() for l in links} == {a.resolve(), b.resolve()}


def test_output_dir_inside_input_is_not_rescanned(scene, key_file, fake_api):
    out = scene / "sorted"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    first = tree(out)
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert tree(out) == first                            # the links weren't sorted as new photos


def test_dry_run_writes_nothing(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "-n", "--api-key-file", key_file) == 0
    assert not out.exists()


# --- the label store: Jev is called once per photo ----------------------------------------------

def test_second_run_sends_nothing_and_needs_no_key(scene, tmp_path, key_file, fake_api, no_default_key):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert len(fake_api.requests) == 6
    assert run_cli(scene, "-o", out) == 0                # every photo cached: no key asked for
    assert len(fake_api.requests) == 6
    lines = [json.loads(l) for l in open(out / "results.jsonl")]
    assert all(l["cached"] and l["input_tokens"] is None for l in lines)
    assert len(tree(out)) == LINKED


def test_only_new_photos_are_sent(scene, tmp_path, key_file, fake_api, make_photo, capsys):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    make_photo("new.jpg", T0 + timedelta(minutes=50), folder=scene, color=(9, 9, 9))
    capsys.readouterr()
    run_cli(scene, "-o", out, "--estimate")
    err = capsys.readouterr().err
    assert "6 already labelled" in err and "1 image(s) to send" in err
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert len(fake_api.requests) == 7                   # only new.jpg (it gets a 500: unknown photo)


def test_labels_can_be_shared_between_output_folders(scene, tmp_path, key_file, fake_api):
    store = tmp_path / "card" / "labels.jsonl"
    run_cli(scene, "-o", tmp_path / "a", "--labels", store, "--api-key-file", key_file)
    run_cli(scene, "-o", tmp_path / "b", "--labels", store, "--api-key-file", key_file)
    assert len(fake_api.requests) == 6 and store.is_file()
    assert tree(tmp_path / "a") == tree(tmp_path / "b")


def test_changed_questions_relabel_and_move_photos(scene, tmp_path, key_file, fake_api, monkeypatch):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    # new wording: the deer turns out to be an elk. Every photo is asked again, and the old folder goes.
    import pythonproject.labels as L
    monkeypatch.setattr(L, "questions_fingerprint", lambda: "new-wording")
    table = {prepare_image(scene / n, crop_banner=True): p for n, (_, p) in SCENE.items()}
    table[prepare_image(scene / "deer.jpg", crop_banner=True)] = {"animal": 0.93, "moose": 0.05, "elk": 0.9}
    fake_api.respond = by_photo(table)
    assert run_cli(scene, "-o", out, "--api-key-file", key_file) == 0
    assert len(fake_api.requests) == 12
    assert "2024-10-30_162804_moose-elk/deer.jpg" in tree(out)
    assert not (out / MIXED).exists()


def test_estimate_prints_cost_and_sends_nothing(scene, tmp_path, fake_api, capsys):
    rc = run_cli(scene, "-o", tmp_path / "out", "--estimate", "--api-key-file", tmp_path / "missing")
    assert rc == 0 and fake_api.requests == []          # no key needed, nothing sent
    err = capsys.readouterr().err
    assert "6 image(s) to send, estimated $" in err
    assert not (tmp_path / "out").exists()


# --- smoothing -----------------------------------------------------------------------------------

@pytest.fixture
def walkaway(make_photo, tmp_path, fake_api):
    """A moose, then the same moose from behind twice: Jev is sure of an animal, but calls it a deer
    (P(moose) 0.3, not the top option)."""
    return make_scene(make_photo, fake_api, tmp_path / "walk", {
        "a.jpg": (0, {"animal": 0.95, "moose": 0.95}),
        "b.jpg": (1, {"animal": 0.95, "moose": 0.3}),
        "c.jpg": (2, {"animal": 0.95, "moose": 0.3}),   # next to b, which is only unsettled: no chain
    })


def labels_of(out):
    return {json.loads(l)["path"].rsplit("/", 1)[1]: json.loads(l) for l in open(out / "results.jsonl")}


def test_smoothing_confirms_moose_next_to_moose(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(walkaway, "-o", out, "--api-key-file", key_file) == 0
    # all three are animals, so all stay in the event; only b becomes moose; c is recorded as unsure
    assert tree(out) == [f"2024-10-30_162804_moose/{n}.jpg" for n in "abc"] + ["unsure/c.jpg"]
    lines = labels_of(out)
    assert [lines[n]["labels"] for n in ("a.jpg", "b.jpg", "c.jpg")] == [["moose"], ["moose"], ["unidentified"]]
    assert lines["b.jpg"]["smoothed"] == ["moose"] and lines["a.jpg"]["smoothed"] == []


def test_no_smooth_flag(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(walkaway, "-o", out, "--no-smooth", "--api-key-file", key_file)
    assert tree(out) == [f"2024-10-30_162804_moose/{n}.jpg" for n in "abc"] + ["unsure/b.jpg", "unsure/c.jpg"]
    assert labels_of(out)["b.jpg"]["labels"] == ["unidentified"] and labels_of(out)["b.jpg"]["unsure"]


def test_event_of_only_unidentified_animals(make_photo, tmp_path, key_file, fake_api):
    fake_api.respond = lambda req: answer(animal=0.95, moose=0.02)   # e.g. a raccoon
    make_photo("raccoon.jpg", T0, folder=tmp_path / "r")
    out = tmp_path / "out"
    assert run_cli(tmp_path / "r", "-o", out, "--api-key-file", key_file) == 0
    assert tree(out) == ["2024-10-30_162804_unidentified/raccoon.jpg", "unsure/raccoon.jpg"]
    [row] = csv.DictReader(open(out / "events.csv"))
    assert (row["type"], row["unidentified"]) == ("other", "1")


# --- unsure/: a record of photos with an animal Jev couldn't identify ------------------------------

def test_unsure_links_survive_export_and_point_to_originals(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(walkaway, "-o", out, "--api-key-file", key_file, "--resolve-links") == 0
    assert tree(out) == ["unsure/c.jpg"]                           # the only symlink left
    assert (out / "unsure/c.jpg").resolve() == (walkaway / "c.jpg").resolve()
    copy = out / "2024-10-30_162804_moose/c.jpg"                   # the photo is still in its event
    assert copy.is_file() and not copy.is_symlink() and keywords_in(copy) == ["unidentified"]
    assert run_cli("--export", out) == 0                           # exporting again leaves it alone
    assert tree(out) == ["unsure/c.jpg"]


def test_zip_stores_unsure_as_symlinks(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(walkaway, "-o", out, "--api-key-file", key_file, "--zip") == 0
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        info = zf.getinfo("out/unsure/c.jpg")
        assert (info.external_attr >> 16) & 0o170000 == 0o120000   # a symlink entry
        assert zf.read(info).decode() == str((walkaway / "c.jpg").resolve())
        event_copy = zf.getinfo("out/2024-10-30_162804_moose/c.jpg")
        assert (event_copy.external_attr >> 16) & 0o170000 != 0o120000          # a real photo
        assert keywords_in(zf.read(event_copy)) == ["unidentified"]


def test_identified_photo_leaves_unsure(walkaway, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(walkaway, "-o", out, "--no-smooth", "--api-key-file", key_file)
    assert "unsure/b.jpg" in tree(out)
    run_cli(walkaway, "-o", out, "--api-key-file", key_file)    # smoothing now makes b a moose
    assert "unsure/b.jpg" not in tree(out) and "unsure/c.jpg" in tree(out)


def test_no_unsure_folder_without_unsure_photos(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert not (out / "unsure").exists()


# --- API key -------------------------------------------------------------------------------------

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


def test_placeholder_key_counts_as_no_key(scene, tmp_path, monkeypatch):
    key = tmp_path / "api_key"
    key.write_text("<put your TypeSafe API key here: create one at https://console.typesafe.ai/>\n")
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key-file", key) == 2   # usage error, nothing sent
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    assert pythonproject.load_api_key(key) == "from-env"


# --- choosing the API key -------------------------------------------------------------------------

@pytest.fixture
def sent_key(monkeypatch):
    """Records the api_key the CLI hands to the SDK client (the fake API ignores it)."""
    import typesafe_sdk
    seen = {}
    patched = typesafe_sdk.AsyncTypeSafeClient.__init__
    def spy(self, **kw):
        seen["key"] = kw.get("api_key")
        patched(self, **kw)
    monkeypatch.setattr(typesafe_sdk.AsyncTypeSafeClient, "__init__", spy)
    return seen


@pytest.fixture
def no_default_key(tmp_path, monkeypatch):
    """No src/api_key and no $TYPESAFE_API_KEY, so only the CLI options can supply a key."""
    monkeypatch.setattr(pythonproject, "DEFAULT_KEY_FILE", tmp_path / "no_default_key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def test_api_key_option_is_sent(scene, tmp_path, sent_key, no_default_key):
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key", " cli-key ") in (0, 1)
    assert sent_key["key"] == "cli-key"


def test_api_key_dash_reads_stdin(scene, tmp_path, sent_key, no_default_key, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-key\n"))
    run_cli(scene, "-o", tmp_path / "out", "-k", "-")
    assert sent_key["key"] == "stdin-key"


def test_api_key_option_beats_env_and_default(scene, tmp_path, sent_key, monkeypatch):
    default = tmp_path / "default_key"
    default.write_text("default-key\n")
    monkeypatch.setattr(pythonproject, "DEFAULT_KEY_FILE", default)
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    run_cli(scene, "-o", tmp_path / "out", "--api-key", "cli-key")
    assert sent_key["key"] == "cli-key"


def test_api_key_and_file_are_mutually_exclusive(scene, tmp_path, key_file, fake_api):
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key", "x", "--api-key-file", key_file) == 2
    assert fake_api.requests == []


def test_empty_api_key_is_usage_error(scene, tmp_path, fake_api, no_default_key):
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key", "  ") == 2
    assert fake_api.requests == []


def test_explicit_missing_key_file_does_not_fall_back_to_env(scene, tmp_path, fake_api, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert run_cli(scene, "-o", tmp_path / "out", "--api-key-file", tmp_path / "missing") == 2
    assert fake_api.requests == []


def test_default_file_then_env(scene, tmp_path, sent_key, no_default_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    run_cli(scene, "-o", tmp_path / "out")
    assert sent_key["key"] == "env-key"


def test_prompts_when_interactive_and_no_key(scene, tmp_path, sent_key, no_default_key, monkeypatch):
    import getpass
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "typed-key")
    run_cli(scene, "-o", tmp_path / "out")
    assert sent_key["key"] == "typed-key"


def test_no_prompt_when_not_interactive(scene, tmp_path, fake_api, no_default_key, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))      # StringIO.isatty() is False
    assert run_cli(scene, "-o", tmp_path / "out") == 2
    assert fake_api.requests == []


# --- resolving links, keywords and zipping the output ----------------------------------------------

def test_resolve_links_copies_originals_with_keywords(scene, tmp_path, key_file):
    out = tmp_path / "out"
    original = (scene / "walker.jpg").read_bytes()
    assert run_cli(scene, "-o", out, "--api-key-file", key_file, "--resolve-links") == 0
    assert tree(out) == []                                    # no symlinks left
    copy = out / OTHER / "walker.jpg"
    assert keywords_in(copy) == ["human", "dog"]
    assert keywords_in(out / MIXED / "deer.jpg") == ["deer"]
    with PILImage.open(copy) as a, PILImage.open(scene / "walker.jpg") as b:
        assert a.tobytes() == b.tobytes()                     # same pixels: the image wasn't re-encoded
        assert a.getexif().get_ifd(0x8769)[0x9003] == "2024:10:30 17:08:04"   # camera EXIF kept
    assert source_size(copy) == len(original)
    assert (scene / "walker.jpg").read_bytes() == original    # original untouched
    assert copy.stat().st_mtime_ns == (scene / "walker.jpg").stat().st_mtime_ns
    assert set(REPORTS) <= set(files_in(out))


def test_resolve_existing_output_without_inputs_sends_nothing(scene, tmp_path, key_file, fake_api):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    sent = len(fake_api.requests)
    links = tree(out)
    assert run_cli("-o", out, "--resolve-links") == 0         # no inputs, no key needed
    assert len(fake_api.requests) == sent
    assert tree(out) == [] and set(links) <= set(files_in(out))
    assert keywords_in(out / MOOSE / "moose2.jpg") == ["moose"]
    assert run_cli("-o", out, "--resolve-links") == 0         # running again is a no-op


def test_rerun_after_resolving_does_not_duplicate(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file, "--resolve-links")
    before = files_in(out)
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    assert tree(out) == [] and files_in(out) == before        # tagged copies still count as placed


def test_missing_labels_copy_without_keywords(scene, tmp_path, key_file, capsys):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    (out / "labels.jsonl").unlink()
    assert run_cli("-o", out, "--resolve-links") == 0
    assert (out / MOOSE / "moose2.jpg").read_bytes() == (scene / "moose2.jpg").read_bytes()
    assert "no labels known for moose2.jpg" in capsys.readouterr().err


def test_zip_contains_tagged_tree(scene, tmp_path, key_file):
    out = tmp_path / "out"
    assert run_cli(scene, "-o", out, "--api-key-file", key_file, "--zip") == 0
    zpath = tmp_path / "out.zip"
    with zipfile.ZipFile(zpath) as zf:
        names = sorted(zf.namelist())
        assert {f"out/{r}" for r in REPORTS} <= set(names)
        data = zf.read(f"out/{MIXED}/moose1.jpg")
    assert keywords_in(data) == ["moose"]
    assert len(names) == len(files_in(out))
    assert not zpath.with_name("out.zip.partial").exists()


def test_zip_path_inside_output_is_not_zipped_into_itself(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    zpath = out / "share.zip"
    assert run_cli("-o", out, "--zip-path", zpath) == 0
    assert run_cli("-o", out, "--zip-path", zpath) == 0       # again, with the old zip present
    with zipfile.ZipFile(zpath) as zf:
        assert not any(n.endswith(".zip") or n.endswith(".partial") for n in zf.namelist())


def test_broken_link_is_reported_and_not_zipped(scene, tmp_path, key_file):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    (scene / "deer.jpg").unlink()
    assert run_cli("-o", out, "--zip") == 1
    assert not (tmp_path / "out.zip").exists()
    assert tree(out) == [f"{MIXED}/deer.jpg"]


def test_export_option_errors(scene, tmp_path, key_file, fake_api):
    out = tmp_path / "out"
    assert run_cli("-o", out) == 2                            # no inputs and nothing to export
    assert run_cli("-o", tmp_path / "missing", "--resolve-links") == 2
    assert run_cli(scene, "-o", out, "-n", "--zip", "--api-key-file", key_file) == 2
    assert fake_api.requests == []


def test_zip_dir_refuses_symlinks(tmp_path):
    from pythonproject.export import zip_dir
    d = tmp_path / "d"
    d.mkdir()
    (tmp_path / "real.jpg").write_bytes(b"x")
    (d / "link.jpg").symlink_to(tmp_path / "real.jpg")
    with pytest.raises(ValueError, match="symlink"):
        zip_dir(d, tmp_path / "d.zip")


# --- --export DIR: resolve (and optionally zip) an earlier run's results --------------------------

@pytest.fixture
def earlier_run(scene, tmp_path, key_file, fake_api):
    """An output folder of symlinks from a previous sort; returns (folder, requests sent so far)."""
    out = tmp_path / "results"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    return out, len(fake_api.requests)


def test_export_resolves_without_api_or_key(earlier_run, fake_api, tmp_path, no_default_key):
    out, sent = earlier_run
    assert run_cli("--export", out) == 0
    assert len(fake_api.requests) == sent
    assert tree(out) == [] and len(files_in(out)) == LINKED + len(REPORTS)
    assert keywords_in(out / OTHER / "walker.jpg") == ["human", "dog"]
    assert not (tmp_path / "results.zip").exists()      # zipping is opt-in


def test_export_with_zip(earlier_run, tmp_path):
    out, _ = earlier_run
    assert run_cli("--export", out, "--zip") == 0
    with zipfile.ZipFile(tmp_path / "results.zip") as zf:
        assert f"results/{MOOSE}/moose2.jpg" in zf.namelist()


def test_export_with_zip_path(earlier_run, tmp_path):
    out, _ = earlier_run
    assert run_cli("--export", out, "--zip-path", tmp_path / "share" / "x.zip") == 0
    assert (tmp_path / "share" / "x.zip").is_file()


def test_export_with_shared_label_store(scene, tmp_path, key_file):
    store = tmp_path / "labels.jsonl"
    out = tmp_path / "results"
    run_cli(scene, "-o", out, "--labels", store, "--api-key-file", key_file)
    assert run_cli("--export", out, "--labels", store) == 0
    assert keywords_in(out / MOOSE / "moose2.jpg") == ["moose"]


def test_export_takes_precedence_over_out(earlier_run, tmp_path):
    out, _ = earlier_run
    assert run_cli("--export", out, "-o", tmp_path / "ignored") == 0
    assert tree(out) == [] and not (tmp_path / "ignored").exists()


def test_export_errors(earlier_run, scene, tmp_path, fake_api):
    out, sent = earlier_run
    assert run_cli("--export", tmp_path / "missing") == 2
    assert run_cli("--export", out, scene) == 2          # photos to sort make no sense here
    assert run_cli("--export", out, "--estimate") == 2
    assert run_cli("--export", out, "--dry-run") == 2
    assert len(fake_api.requests) == sent
    assert len(tree(out)) == LINKED                      # nothing was resolved by the failed calls
