"""Load moose-sort settings from an HCL file.

    inputs      = ["~/trailcam/card-12"]
    output      = "sorted"
    events      = "static"
    smoothing   = true
    concurrency = 8

    api_key_file = "~/.config/typesafe/key"

    export {
      resolve_links = true
      zip           = true
      zip_path      = "share/moose.zip"
    }

Command-line options override the file; the file overrides the built-in defaults.
Relative paths are resolved against the config file's directory, and "~" is expanded.
"""

from dataclasses import dataclass
from pathlib import Path

import hcl2
from hcl2 import SerializationOptions
from lark.exceptions import LarkError

DEFAULT_CONFIG_NAME = "moose-sort.hcl"   # picked up from the current directory when --config isn't given

_PARSE = SerializationOptions(strip_string_quotes=True, explicit_blocks=False, with_comments=False)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    dest: str          # argparse dest it feeds
    kind: type         # str, bool, int, Path, or list (of paths)
    choices: tuple = ()


# top-level attribute -> setting, and the attributes allowed inside `export { ... }`
TOP: dict[str, Setting] = {
    "inputs": Setting("inputs", list),
    "output": Setting("out", Path),
    "events": Setting("events", str, ("rolling", "static")),
    "smoothing": Setting("smooth", bool),
    "concurrency": Setting("concurrency", int),
    "api_key": Setting("api_key", str),
    "api_key_file": Setting("api_key_file", Path),
}
EXPORT: dict[str, Setting] = {
    "resolve_links": Setting("resolve_links", bool),
    "zip": Setting("zip", bool),
    "zip_path": Setting("zip_path", Path),
}


def _path(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def _convert(name: str, setting: Setting, value, base: Path):
    def bad(expected: str):
        return ConfigError(f"{name}: expected {expected}, got {value!r}")

    if isinstance(value, str) and "${" in value:
        raise ConfigError(f"{name}: interpolation (${{...}}) isn't supported; write the value literally")
    if setting.kind is bool:
        if not isinstance(value, bool):
            raise bad("true or false")
        return value
    if setting.kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("a whole number")
        if value < 1:
            raise ConfigError(f"{name}: must be at least 1, got {value}")
        return value
    if setting.kind is str:
        if not isinstance(value, str) or not value.strip():
            raise bad("a non-empty string")
        if setting.choices and value not in setting.choices:
            raise ConfigError(f"{name}: must be one of {', '.join(setting.choices)}, got {value!r}")
        return value.strip()
    if setting.kind is Path:
        if not isinstance(value, str) or not value.strip():
            raise bad("a path string")
        return _path(value, base)
    if setting.kind is list:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
            raise bad("a list of path strings")
        return [_path(v, base) for v in value]
    raise AssertionError(setting)


def parse_config(text: str, base: Path, source: str = "<config>") -> dict:
    """Parse HCL text into {argparse dest: value}. Raises ConfigError on any problem."""
    try:
        raw = hcl2.loads(text, serialization_options=_PARSE)
    except LarkError as e:
        first = str(e).strip().splitlines()[0]
        raise ConfigError(f"{source}: not valid HCL: {first}") from None

    out: dict = {}

    def take(items: dict, table: dict[str, Setting], where: str) -> None:
        for name, value in items.items():
            if name not in table:
                allowed = sorted(table) + ([] if where else ["export { ... }"])
                raise ConfigError(f"{source}: unknown setting '{where}{name}'; "
                                  f"allowed: {', '.join(allowed)}")
            try:
                out[table[name].dest] = _convert(where + name, table[name], value, base)
            except ConfigError as e:
                raise ConfigError(f"{source}: {e}") from None

    raw = dict(raw)
    export = raw.pop("export", None)
    take(raw, TOP, "")
    if export is not None:
        blocks = export if isinstance(export, list) else [export]
        if len(blocks) != 1 or not isinstance(blocks[0], dict):
            raise ConfigError(f"{source}: expected exactly one `export {{ ... }}` block")
        take(blocks[0], EXPORT, "export.")

    if "api_key" in out and "api_key_file" in out:
        raise ConfigError(f"{source}: set api_key or api_key_file, not both")
    return out


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return parse_config(path.read_text(), base=path.resolve().parent, source=str(path))


def find_config(explicit: Path | None, cwd: Path) -> Path | None:
    """--config PATH if given, else ./moose-sort.hcl if it exists, else None."""
    if explicit is not None:
        return explicit
    default = cwd / DEFAULT_CONFIG_NAME
    return default if default.is_file() else None
