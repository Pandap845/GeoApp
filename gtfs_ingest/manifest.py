"""run_manifest.json: what produced the files in an output folder.

One entry per command, holding the input file hashes, the git commit, a hash
of the package code, the package versions and every parameter value. There is no timestamp, so two runs
with equal manifests must give equal outputs. Secrets are never written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import subprocess
from collections.abc import Iterable, Mapping
from datetime import date
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

MANIFEST_NAME = "run_manifest.json"

# Third-party packages whose version can change the results.
TRACKED_PACKAGES = (
    "pandas",
    "partridge",
    "networkx",
    "scipy",
    "scikit-learn",
    "shapely",
    "geopandas",
    "SQLAlchemy",
    "pydantic",
)

# Parameters dropped from the manifest: secrets, settings that only say where
# files are or how much is logged, and input paths (the inputs list has them).
_SECRET_PARTS = ("database_url", "db_url", "api_key", "password", "token", "secret")
_IGNORED_KEYS = {"report_dir", "log_level", "feed_paths", "stations_file"}

_CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    """Hex sha256 of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _input_entry(path: Path) -> dict[str, Any]:
    """Hash of one input; a folder hashes its files' names and contents.

    Only the name is kept, so the same files give the same entry on any machine.
    """
    if path.is_file():
        return {"name": path.name, "sha256": file_sha256(path), "bytes": path.stat().st_size}
    digest = hashlib.sha256()
    total = 0
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(file_sha256(child).encode())
        total += child.stat().st_size
    return {"name": path.name, "sha256": digest.hexdigest(), "bytes": total}


def _git(*args: str) -> str | None:
    """Output of a git command run in the package folder, or None."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def git_state() -> dict[str, Any] | None:
    """Current commit and whether tracked files have local changes."""
    commit = _git("rev-parse", "HEAD")
    if not commit:
        return None
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "dirty": bool(status)}


def code_sha256() -> str:
    """Hash of the package's .py files, so uncommitted code changes show up too.

    Line endings are normalized, so Windows and Linux checkouts match.
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in TRACKED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _is_hidden(key: str) -> bool:
    lowered = key.lower()
    return lowered in _IGNORED_KEYS or any(part in lowered for part in _SECRET_PARTS)


def clean_parameters(value: Any) -> Any:
    """JSON-ready copy of *value* without secrets; sets become sorted lists."""
    if isinstance(value, Mapping):
        return {str(k): clean_parameters(v) for k, v in value.items() if not _is_hidden(str(k))}
    if isinstance(value, (set, frozenset)):
        return sorted(clean_parameters(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [clean_parameters(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def build_manifest(
    command: str, parameters: Mapping[str, Any], inputs: Iterable[Path] = ()
) -> dict[str, Any]:
    """Manifest entry for one command run."""
    return {
        "command": command,
        "package_version": __version__,
        "code_sha256": code_sha256(),
        "python": platform.python_version(),
        "packages": _package_versions(),
        "git": git_state(),
        "inputs": [_input_entry(Path(p)) for p in inputs],
        "parameters": clean_parameters(parameters),
    }


def write_manifest(report_dir: Path, entry: Mapping[str, Any]) -> Path:
    """Add or replace this command's entry in report_dir/run_manifest.json."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / MANIFEST_NAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("%s is not valid JSON; rewriting it", path)
    data[entry["command"]] = entry
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
