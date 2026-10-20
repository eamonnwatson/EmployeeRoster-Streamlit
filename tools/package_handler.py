"""Purely local, deterministic handler archive. Never deploys or reads credentials.

Usage: python tools/package_handler.py --output <existing-directory>/employee_roster.zip
The allowlist excludes Excel/UI/demo/tests/config files and third-party packages.
Snowflake installs snowflake-snowpark-python and regex through SQL PACKAGES.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "__init__.py", "domain.py", "authorization.py", "persistence.py", "directory.py", "handler.py",
    "services/__init__.py", "services/operations.py", "services/uploads.py",
)


def build(output: Path) -> Path:
    output = output.resolve()
    if not output.parent.is_dir() or output.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip in an existing directory.")
    if output.exists():
        raise FileExistsError("Refusing to replace an existing archive; choose a new path.")
    package = ROOT / "src" / "employee_roster"
    sources = [(name, (package / name).read_bytes()) for name in PACKAGE_FILES]
    # Preflight everything before creating an artifact; no imports or side effects.
    for name, content in sources:
        compile(content, name, "exec")
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in sources:
            info = ZipInfo("employee_roster/" + name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build(arguments.output))
