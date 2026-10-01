"""One absolute data root, independent of the launching working directory."""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("HUIYE_DATA_DIR", PROJECT_DIR / "data/test")).expanduser().resolve()
CONFIG_DIR = Path(os.environ.get("HUIYE_CONFIG_DIR", PROJECT_DIR / "config")).expanduser().resolve()
LOG_DIR = DATA_DIR / "logs"
NOTE_DIR = DATA_DIR / "notes"

def data_path(name):
    return str(DATA_DIR / name)

def atomic_json(path, data):
    import json
    import tempfile
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
