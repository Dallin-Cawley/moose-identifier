"""HCL config file: parsing, validation, and precedence against the command line."""

from pathlib import Path

import pytest

import pythonproject
from pythonproject.config import ConfigError, find_config, parse_config
from test_cli import (LINKED, MOOSE, REPORTS, run_cli, tree, files_in, scene, key_file,  # noqa: F401
                      isolated_cwd, sent_key, no_default_key)

BASE = Path("/base")


def test_full_example_parses():
    cfg = parse_config('''
        # trail camera on the north ridge
        inputs      = ["photos", "~/more", "/abs/card"]
        output      = "sorted_ridge"
        labels      = "ridge/labels.jsonl"
        smoothing   = false
        concurrency = 4
        api_key_file = "keys/typesafe"   // relative to the config file

        export {
          resolve_links = true
          zip           = true
          zip_path      = "share/moose.zip"
        }
    ''', BASE)
    assert cfg == {
        "inputs": [BASE / "photos", Path("~/more").expanduser(), Path("/abs/card")],
        "out": BASE / "sorted_ridge",
        "labels": BASE / "ridge/labels.jsonl",
        "smooth": False,
        "concurrency": 4,
        "api_key_file": BASE / "keys/typesafe",
        "resolve_links": True,
        "zip": True,
        "zip_path": BASE / "share/moose.zip",
    }


def test_empty_config_is_fine():
    assert parse_config("", BASE) == {}


def test_single_input_string_is_accepted():
    assert parse_config('inputs = "photos"', BASE) == {"inputs": [BASE / "photos"]}


@pytest.mark.parametrize("text, message", [
    ('outptu = "x"', "unknown setting 'outptu'"),
    ('export {\n  zipp = true\n}', "unknown setting 'export.zipp'"),
    ('events = "static"', "setting 'events' was removed: events are now defined by animal activity"),
    ('labels = ""', "expected a path string"),
    ('smoothing = "no"', "expected true or false"),
    ('concurrency = "8"', "expected a whole number"),
    ('concurrency = 0', "must be at least 1"),
    ('concurrency = true', "expected a whole number"),
    ('inputs = []', "expected a list of path strings"),
    ('inputs = [1]', "expected a list of path strings"),
    ('output = ""', "expected a path string"),
    ('output = "${HOME}/x"', "interpolation"),
    ('api_key = "a"\napi_key_file = "b"', "not both"),
    ('export {\n  zip = true\n}\nexport {\n  zip = false\n}', "exactly one `export"),
    ('output = ', "not valid HCL"),
])
def test_invalid_configs(text, message):
    with pytest.raises(ConfigError, match=message.replace("$", r"\$").replace("{", r"\{").replace("}", r"\}")):
        parse_config(text, BASE)


def test_find_config(tmp_path):
    assert find_config(None, tmp_path) is None
    (tmp_path / "moose-sort.hcl").write_text("")
    assert find_config(None, tmp_path) == tmp_path / "moose-sort.hcl"
    assert find_config(Path("other.hcl"), tmp_path) == Path("other.hcl")


# --- through the CLI ------------------------------------------------------------------------------

def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_config_supplies_inputs_output_and_key(scene, tmp_path, key_file, isolated_cwd):
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{scene}"]\noutput = "out"\napi_key_file = "{key_file}"\n')
    assert run_cli("--config", cfg) == 0
    assert len(tree(tmp_path / "out")) == LINKED     # output is relative to the config file's folder


def test_default_config_in_cwd_is_used(scene, tmp_path, key_file, isolated_cwd):
    write(isolated_cwd / "moose-sort.hcl", f'inputs = ["{scene}"]\napi_key_file = "{key_file}"\n')
    assert run_cli() == 0
    assert len(tree(isolated_cwd / "sorted")) == LINKED


def test_no_config_ignores_default_file(scene, tmp_path, isolated_cwd, fake_api):
    write(isolated_cwd / "moose-sort.hcl", f'inputs = ["{scene}"]\n')
    assert run_cli("--no-config") == 2               # no inputs anymore
    assert fake_api.requests == []


def test_command_line_overrides_config(scene, tmp_path, key_file):
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{scene}"]\noutput = "cfg_out"\nlabels = "cfg_labels.jsonl"\n'
                                     f'api_key_file = "{key_file}"\n')
    out = tmp_path / "cli_out"
    run_cli("--config", cfg, "-o", out, "--labels", tmp_path / "cli_labels.jsonl")
    assert not (tmp_path / "cfg_out").exists() and not (tmp_path / "cfg_labels.jsonl").exists()
    assert len(tree(out)) == LINKED and (tmp_path / "cli_labels.jsonl").is_file()


def test_smoothing_false_in_config_and_smooth_flag(tmp_path, key_file, fake_api, make_photo):
    from test_cli import labels_of, make_scene
    walk = make_scene(make_photo, fake_api, tmp_path / "walk",
                      {"a.jpg": (0, {"animal": 0.95, "moose": 0.95}), "b.jpg": (1, {"animal": 0.95, "moose": 0.3})})
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{walk}"]\nsmoothing = false\n'
                                     f'api_key_file = "{key_file}"\n')
    run_cli("--config", cfg, "-o", tmp_path / "off")
    assert labels_of(tmp_path / "off")["b.jpg"]["labels"] == ["unidentified"]
    run_cli("--config", cfg, "-o", tmp_path / "on", "--smooth")
    assert labels_of(tmp_path / "on")["b.jpg"]["labels"] == ["moose"]


def test_cli_inputs_replace_config_inputs(scene, tmp_path, key_file, make_photo, fake_api):
    other = tmp_path / "other"
    other.mkdir()
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{other}"]\napi_key_file = "{key_file}"\n')
    run_cli("--config", cfg, scene, "-o", tmp_path / "out")
    assert len(tree(tmp_path / "out")) == LINKED


def test_cli_key_beats_config_key(scene, tmp_path, sent_key, no_default_key):
    cfg = write(tmp_path / "ms.hcl", 'api_key = "config-key"\n')
    run_cli("--config", cfg, scene, "-o", tmp_path / "a")
    assert sent_key["key"] == "config-key"
    keyf = write(tmp_path / "k", "file-key\n")
    run_cli("--config", cfg, scene, "-o", tmp_path / "b", "--api-key-file", keyf)
    assert sent_key["key"] == "file-key"           # config api_key must not win over a CLI key file


def test_config_export_block_zips(scene, tmp_path, key_file):
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{scene}"]\napi_key_file = "{key_file}"\n'
                                     'export {\n  zip_path = "share/moose.zip"\n}\n')
    assert run_cli("--config", cfg, "-o", tmp_path / "out") == 0
    assert (tmp_path / "share/moose.zip").is_file() and tree(tmp_path / "out") == []


def test_no_zip_flag_overrides_config(scene, tmp_path, key_file):
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{scene}"]\napi_key_file = "{key_file}"\n'
                                     'export {\n  zip = true\n  zip_path = "share/moose.zip"\n}\n')
    run_cli("--config", cfg, "-o", tmp_path / "out", "--no-zip")
    assert not (tmp_path / "share/moose.zip").exists()
    assert len(tree(tmp_path / "out")) == LINKED   # no export at all: still links


def test_export_ignores_config_inputs(scene, tmp_path, key_file, fake_api):
    out = tmp_path / "out"
    run_cli(scene, "-o", out, "--api-key-file", key_file)
    sent = len(fake_api.requests)
    cfg = write(tmp_path / "ms.hcl", f'inputs = ["{scene}"]\noutput = "elsewhere"\n')
    assert run_cli("--config", cfg, "--export", out) == 0
    assert len(fake_api.requests) == sent          # nothing sent
    assert tree(out) == [] and len(files_in(out)) == LINKED + len(REPORTS)


def test_bad_config_is_usage_error(tmp_path, fake_api):
    assert run_cli("--config", write(tmp_path / "bad.hcl", 'outptu = "x"')) == 2
    assert run_cli("--config", tmp_path / "missing.hcl") == 2
    assert fake_api.requests == []


def test_readme_example_config_is_valid():
    """The example in README.md must parse and set every documented setting."""
    import re
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    [example] = re.findall(r"```hcl\n(.*?)```", readme, re.S)
    cfg = parse_config(example, BASE)
    assert set(cfg) == {"inputs", "out", "labels", "smooth", "concurrency", "api_key_file",
                        "resolve_links", "zip", "zip_path"}
