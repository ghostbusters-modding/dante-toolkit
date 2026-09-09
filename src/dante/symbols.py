"""Loading of the packaged symbol databases, and the env vars that override them."""
import os
import json

try:
    from importlib.resources import files as _res_files
except ImportError:                     # pragma: no cover - py3.8
    _res_files = None

API_JSON = "dante_api.json"
SYMBOLS_JSON = "dante_symbols.json"


def data_dir():
    """The packaged `data/` directory, as a filesystem path."""
    if _res_files is not None:
        try:
            return os.fspath(_res_files(__package__) / "data")
        except (TypeError, NotImplementedError):   # pragma: no cover - zip import
            pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def data_path(name):
    """Path to one packaged data file, or None when it is not installed."""
    p = os.path.join(data_dir(), name)
    return p if os.path.exists(p) else None


def api_json_path():
    """The API table: $DANTE_API_JSON, else the packaged one, else None."""
    env = os.environ.get("DANTE_API_JSON")
    if env:
        return env if os.path.exists(env) else None
    return data_path(API_JSON)


def env_symbol_paths():
    """Extra symbol JSON named by $DANTE_SYMBOLS (os.pathsep-separated)."""
    raw = os.environ.get("DANTE_SYMBOLS", "")
    return [p for p in raw.split(os.pathsep) if p and os.path.exists(p)]


def default_symbol_sources():
    """The sources SymbolDB loads, as (kind, path) with kind "api" or "json"."""
    out = []
    api = api_json_path()
    if api:
        out.append(("api", api))
    sym = data_path(SYMBOLS_JSON)
    if sym:
        out.append(("json", sym))
    out += [("json", p) for p in env_symbol_paths()]
    return out


_API = None


def api_table():
    """The parsed API table (cached), or {} when none is installed."""
    global _API
    if _API is None:
        p = api_json_path()
        try:
            with open(p, encoding="utf-8") as fh:
                _API = json.load(fh)
        except Exception:
            _API = {}
    return _API
