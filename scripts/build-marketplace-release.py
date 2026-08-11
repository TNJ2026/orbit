#!/usr/bin/env python3
"""Build a self-contained local Codex Marketplace archive for Orbit."""

from __future__ import annotations

import argparse
import ast
import json
import re
import stat
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = Path("orbit-marketplace")
PLUGIN_ROOT = ARCHIVE_ROOT / "plugins" / "orbit"
INCLUDE_FILES = (
    ".mcp.json",
    "README.md",
    "README.zh-CN.md",
    "agent-app.json",
    "pyproject.toml",
    "uv.lock",
)
INCLUDE_DIRS = (".codex-plugin", "scripts", "skills", "src")
EXCLUDED_PARTS = {"__pycache__", ".DS_Store"}
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def package_version() -> str:
    tree = ast.parse((ROOT / "src/orbit/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit("src/orbit/__init__.py does not define __version__")


def base_version(value: str) -> str:
    return value.split("+", 1)[0]


def marketplace_document() -> dict[str, object]:
    return {
        "name": "orbit-local",
        "interface": {"displayName": "Orbit Local"},
        "plugins": [
            {
                "name": "orbit",
                "source": {"source": "local", "path": "./plugins/orbit"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }


def source_files() -> list[Path]:
    files = [ROOT / name for name in INCLUDE_FILES]
    for directory in INCLUDE_DIRS:
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    selected = [
        path
        for path in files
        if not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
        and path.suffix not in {".pyc", ".pyo"}
    ]
    missing = [str(path.relative_to(ROOT)) for path in files if not path.exists()]
    if missing:
        raise SystemExit(f"required release files are missing: {', '.join(missing)}")
    return sorted(set(selected), key=lambda path: path.as_posix())


def zip_info(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build(output: Path, version: str | None) -> None:
    manifest_path = ROOT / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_version = package_version()
    manifest_version = str(manifest["version"])
    if base_version(manifest_version) != base_version(source_version):
        raise SystemExit(
            "plugin and package base versions differ: "
            f"{manifest_version!r} != {source_version!r}"
        )
    release_version = version or source_version
    if not SEMVER.fullmatch(release_version):
        raise SystemExit(f"release version is not valid semver: {release_version!r}")
    if version is not None and release_version != source_version:
        raise SystemExit(
            f"requested release version {release_version!r} differs from package "
            f"version {source_version!r}"
        )
    manifest["version"] = release_version

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        marketplace_name = ARCHIVE_ROOT / ".agents" / "plugins" / "marketplace.json"
        archive.writestr(
            zip_info(marketplace_name.as_posix()),
            json.dumps(marketplace_document(), ensure_ascii=False, indent=2) + "\n",
        )
        for source in source_files():
            relative = source.relative_to(ROOT)
            destination = PLUGIN_ROOT / relative
            if relative == Path(".codex-plugin/plugin.json"):
                payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                archive.writestr(zip_info(destination.as_posix()), payload)
                continue
            mode = 0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644
            archive.writestr(zip_info(destination.as_posix(), mode), source.read_bytes())

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        required = {
            (ARCHIVE_ROOT / ".agents/plugins/marketplace.json").as_posix(),
            (PLUGIN_ROOT / ".codex-plugin/plugin.json").as_posix(),
            (PLUGIN_ROOT / ".mcp.json").as_posix(),
            (PLUGIN_ROOT / "skills/orbit/SKILL.md").as_posix(),
        }
        missing = required - names
        if missing:
            raise SystemExit(f"marketplace archive is incomplete: {sorted(missing)}")
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", help="release semver written into plugin.json")
    args = parser.parse_args()
    build(args.output.resolve(), args.version)


if __name__ == "__main__":
    main()
