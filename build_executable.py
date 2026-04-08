from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _next_build_name(dist_dir: Path, prefix: str) -> str:
    versions: list[int] = []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{3}})$")
    for path in dist_dir.iterdir():
        if not path.is_dir():
            continue
        match = pattern.match(path.name)
        if match:
            versions.append(int(match.group(1)))
    next_version = (max(versions) + 1) if versions else 1
    return f"{prefix}{next_version:03d}"


def _add_data_arg() -> str:
    separator = ";" if sys.platform.startswith("win") else ":"
    return f"config{separator}config"


def _output_path(root: Path, build_name: str) -> Path:
    dist_dir = root / "dist"
    if sys.platform == "darwin":
        return dist_dir / f"{build_name}.app"
    if sys.platform.startswith("win"):
        return dist_dir / build_name / f"{build_name}.exe"
    return dist_dir / build_name / build_name


def main() -> int:
    root = Path(__file__).resolve().parent
    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    build_name = "SmartInventory"
    print(f"Building {build_name} ...")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        build_name,
        "--add-data",
        _add_data_arg(),
        "main.py",
    ]
    env = os.environ.copy()
    if sys.platform == "darwin":
        env["COPYFILE_DISABLE"] = "1"
    subprocess.run(command, cwd=root, env=env, check=True)

    temp_spec = root / f"{build_name}.spec"
    if temp_spec.exists():
        temp_spec.unlink()

    output = _output_path(root, build_name)
    print("")
    print("Build complete:")
    print(f"  {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
