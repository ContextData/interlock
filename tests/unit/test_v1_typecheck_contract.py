from __future__ import annotations

import configparser
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "typecheck" / "v1_targets.toml"
CONFIG_PATH = ROOT / "mypy-v1.ini"


def _manifest() -> dict[str, object]:
    with MANIFEST_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _area_files(area: dict[str, object]) -> set[str]:
    files: set[str] = set()
    for raw_path in area["paths"]:  # type: ignore[index]
        path = ROOT / str(raw_path)
        if path.is_dir():
            files.update(item.relative_to(ROOT).as_posix() for item in path.rglob("*.py"))
        else:
            files.add(path.relative_to(ROOT).as_posix())
    return files


def test_v1_typecheck_covers_each_required_runtime_area() -> None:
    manifest = _manifest()
    areas = {area["name"]: area for area in manifest["areas"]}  # type: ignore[index]

    # `catalog` was added with the source catalog at a zero-error budget: new
    # code starts strict rather than being brought up to it later.
    assert set(areas) == {"gateway", "admin", "worker", "stable_connectors", "catalog"}
    assert len(_area_files(areas["catalog"])) >= 8
    assert len(_area_files(areas["gateway"])) >= 8
    assert len(_area_files(areas["admin"])) >= 15
    assert len(_area_files(areas["worker"])) >= 15
    assert len(_area_files(areas["stable_connectors"])) >= 6

    for name, area in areas.items():
        covered = _area_files(area)
        strict_files = set(area["strict_files"])
        assert strict_files, f"{name} must retain a strict, debt-free allowlist"
        assert strict_files <= covered
        assert all((ROOT / path).is_file() for path in strict_files)


def test_v1_mypy_config_has_no_global_error_suppression() -> None:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH)

    global_config = parser["mypy"]
    assert global_config.getboolean("strict") is True
    assert global_config.getboolean("ignore_missing_imports", fallback=False) is False
    assert "ignore_errors" not in global_config
    assert "disable_error_code" not in global_config

    for section in parser.sections():
        assert parser[section].getboolean("ignore_errors", fallback=False) is False


def test_v1_typecheck_baseline_is_bounded_and_only_tracks_covered_debt() -> None:
    manifest = _manifest()
    areas = list(manifest["areas"])  # type: ignore[arg-type]
    covered = {path for area in areas for path in _area_files(area)}
    strict_files = {str(path) for area in areas for path in area["strict_files"]}
    baseline_path = ROOT / str(manifest["baseline"])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    entries = baseline["errors"]

    assert baseline["schema_version"] == 1
    assert entries == sorted(
        entries,
        key=lambda item: (item["path"], item["code"], item["message"]),
    )
    counts_by_area = {area["name"]: 0 for area in areas}
    for item in entries:
        for area in areas:
            if item["path"] in _area_files(area):
                counts_by_area[area["name"]] += int(item["count"])
                break

    # Pinned totals, so debt cannot creep in unnoticed. Lowering these is the
    # only direction that should ever be easy: gateway went 39 -> 38 when a
    # dict-typed variable stopped being rebound to a list in
    # MCPAdapter._describe_source. Raising one needs a deliberate edit here.
    assert sum(int(item["count"]) for item in entries) == 126
    assert counts_by_area == {
        "gateway": 38,
        "admin": 49,
        "worker": 0,
        "stable_connectors": 39,
        "catalog": 0,
    }
    assert all(counts_by_area[area["name"]] <= int(area["max_baseline_errors"]) for area in areas)
    assert all(item["path"] in covered for item in entries)
    assert all(item["path"] not in strict_files for item in entries)
    assert all(item["count"] > 0 for item in entries)


def test_v1_typecheck_script_does_not_mutate_baseline_by_default() -> None:
    script = (ROOT / "tools" / "typecheck_v1.py").read_text(encoding="utf-8")

    assert "--update-baseline" in script
    assert "args.update_baseline" in script
    assert "write_text" in script
    assert "ignore_errors" not in script
