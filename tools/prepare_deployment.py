"""Prepare a reviewed deployment bundle offline; never connect or deploy.

All resource flags are required. Source files are copied from a fixed allowlist,
never imported, executed, rewritten, or recursively uploaded. See DEPLOYMENT.md.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    "streamlit_app.py",
    "app_pages/clients.py",
    "app_pages/identifiers.py",
    "app_pages/roster.py",
    "app_pages/upload.py",
    "src/employee_roster/__init__.py",
    "src/employee_roster/authorization.py",
    "src/employee_roster/directory.py",
    "src/employee_roster/domain.py",
    "src/employee_roster/excel.py",
    "src/employee_roster/handler.py",
    "src/employee_roster/persistence.py",
    "src/employee_roster/services/__init__.py",
    "src/employee_roster/services/operations.py",
    "src/employee_roster/services/uploads.py",
    "src/employee_roster/ui.py",
    ".streamlit/config.toml",
    "pyproject.toml",
)
RESOURCE_NAMES = ("warehouse", "compute_pool", "pypi_eai", "owner_role", "viewer_role")
SYSTEM_ROLES = {"PUBLIC", "ACCOUNTADMIN", "ORGADMIN", "GLOBALORGADMIN", "SECURITYADMIN", "USERADMIN", "SYSADMIN"}
# Conservatively reject both reserved and limited keywords (Snowflake SQL reference).
RESERVED_WORDS = set("""
ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONNECTION
CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER
DATABASE DELETE DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER
HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL LEFT LIKE LOCALTIME
LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION QUALIFY REGEXP REVOKE RIGHT
RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST
UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHENEVER WHERE WINDOW WITH
""".split())


def identifier(value: str) -> str:
    """Accept only single, ASCII, unquoted identifiers; never SQL fragments."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,254}", value):
        raise ValueError("Resources must be single unquoted Snowflake identifiers (1-255 ASCII characters).")
    normalized = value.upper()
    if normalized in RESERVED_WORDS:
        raise ValueError("Reserved or limited SQL keywords are not supported as resource identifiers.")
    return normalized


def connection_alias(value: str) -> str:
    # An alias is local CLI configuration, not a SQL identifier or a credential.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,127}", value):
        raise ValueError("Connection alias must use letters, digits, spaces, underscores, dots or hyphens.")
    if value != value.strip():
        raise ValueError("Connection alias must not have surrounding whitespace.")
    return value


def manifest(resources: dict[str, str]) -> str:
    lines = [
        "# Generated offline. Resource existence, access and runtime behavior are NOT verified.",
        "# Deploy manually from this directory as the owner in deployment.json, after review.",
        "definition_version: 2",
        "entities:",
        "  employee_roster:",
        "    type: streamlit",
        "    identifier:",
        "      name: EMPLOYEE_ROSTER",
        "      database: DATASTORE",
        "      schema: ROSTER",
        "    runtime_name: SYSTEM$ST_CONTAINER_RUNTIME_PY3_11",
        f"    query_warehouse: {json.dumps(resources['warehouse'])}",
        f"    compute_pool: {json.dumps(resources['compute_pool'])}",
        "    external_access_integrations:",
        f"      - {json.dumps(resources['pypi_eai'])}",
        "    main_file: streamlit_app.py",
        "    artifacts:",
        *(f"      - {path}" for path in ARTIFACTS),
    ]
    return "\n".join(lines) + "\n"


def owner_grants(resources: dict[str, str]) -> str:
    owner = resources["owner_role"]
    return f"""-- MANUAL ADMIN REVIEW ONLY. Never executed by the generator.
-- Run after namespace and ROSTER_EXECUTE creation; before owner deployment.
-- Existing dedicated app owner; no role creation, assignment or ownership transfer.
-- Controlled schema/procedure owner separately retains private table, stage and
-- ENGAGEHR.ENGAGEHR.GETEMPLOYEES access. See DEPLOYMENT.md.
-- Managed-access schema grants must be applied by its authorized administrator.
GRANT USAGE ON DATABASE DATASTORE TO ROLE {owner};
GRANT USAGE ON SCHEMA DATASTORE.ROSTER TO ROLE {owner};
GRANT CREATE STREAMLIT ON SCHEMA DATASTORE.ROSTER TO ROLE {owner};
GRANT USAGE ON WAREHOUSE {resources['warehouse']} TO ROLE {owner};
GRANT USAGE ON COMPUTE POOL {resources['compute_pool']} TO ROLE {owner};
GRANT USAGE ON INTEGRATION {resources['pypi_eai']} TO ROLE {owner};
GRANT USAGE ON PROCEDURE DATASTORE.ROSTER.ROSTER_EXECUTE(VARCHAR, VARCHAR, VARCHAR) TO ROLE {owner};
"""


def viewer_grants(resources: dict[str, str]) -> str:
    viewer = resources["viewer_role"]
    return f"""-- MANUAL ADMIN REVIEW ONLY. Apply ONLY after deployment and acceptance gates.
-- No table, procedure, warehouse, compute-pool, integration, stage or owner-role
-- access is granted to viewers. Never fix app errors by broadening viewer access.
-- Inspect inherited/secondary/PUBLIC/future grants separately before sharing.
GRANT USAGE ON DATABASE DATASTORE TO ROLE {viewer};
GRANT USAGE ON SCHEMA DATASTORE.ROSTER TO ROLE {viewer};
GRANT USAGE ON STREAMLIT DATASTORE.ROSTER.EMPLOYEE_ROSTER TO ROLE {viewer};
"""


def build(output: Path, *, warehouse: str, compute_pool: str, pypi_eai: str,
          owner_role: str, viewer_role: str, connection: str, source_root: Path = ROOT) -> Path:
    """Validate all inputs before creating a new bundle; refuse any overwrite."""
    resources = dict(zip(RESOURCE_NAMES, map(identifier, (
        warehouse, compute_pool, pypi_eai, owner_role, viewer_role,
    ))))
    resources["connection"] = connection_alias(connection)
    if resources["owner_role"] == resources["viewer_role"]:
        raise ValueError("Owner and viewer roles must be distinct.")
    if {resources["owner_role"], resources["viewer_role"]} & SYSTEM_ROLES:
        raise ValueError("Use dedicated owner and viewer roles, not built-in administrative or PUBLIC roles.")
    root = source_root.resolve(strict=True)
    output = Path(output)
    if not output.is_absolute():
        raise ValueError("Output must be an absolute path in an existing directory.")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Refusing to replace an existing bundle; choose a new path.")
    output = output.resolve()
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist.")
    if output == root or (output.is_relative_to(root) and output.relative_to(root).parts[0] not in {"deployment", "dist"}):
        raise ValueError("Inside the source tree, bundles must be under deployment/ or dist/.")

    sources = {}
    for relative in ARTIFACTS:
        source = root / relative
        if any(part.is_symlink() for part in (source, *source.parents) if part != root and part.is_relative_to(root)):
            raise ValueError(f"Symlink artifacts are forbidden: {relative}")
        if not source.resolve().is_relative_to(root) or not source.is_file():
            raise ValueError(f"Missing or unsafe artifact: {relative}")
        content = source.read_bytes()
        if source.suffix == ".py":
            ast.parse(content, filename=relative)
        sources[relative] = content
    production_python = {path.relative_to(root).as_posix()
                         for directory in (root / "src", root / "app_pages")
                         for path in directory.rglob("*.py")}
    if production_python - set(ARTIFACTS):
        raise ValueError("Unlisted production Python files; review and update the explicit artifact allowlist.")

    sources["snowflake.yml"] = manifest(resources).encode("utf-8")
    sources["grants_owner.sql"] = owner_grants(resources).encode("utf-8")
    sources["grants_viewer.sql"] = viewer_grants(resources).encode("utf-8")
    sources["deployment.json"] = (json.dumps({
        "target": "DATASTORE.ROSTER.EMPLOYEE_ROSTER",
        "status": "offline-prepared-not-live-verified",
        **resources,
    }, indent=2) + "\n").encode("utf-8")
    # mkdir is exclusive. Only this newly created output is removed on failure.
    output.mkdir()
    try:
        for relative, content in sources.items():
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
    except Exception:
        shutil.rmtree(output)
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    for name in RESOURCE_NAMES:
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    parser.add_argument("--connection", required=True, help="Explicit local CLI alias; no configuration is read.")
    arguments = parser.parse_args(argv)
    try:
        output = build(**vars(arguments))
    except (OSError, ValueError, SyntaxError) as exc:
        parser.error(str(exc))
    print(f"Offline bundle prepared: {output}. Review grants and DEPLOYMENT.md; nothing was deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
