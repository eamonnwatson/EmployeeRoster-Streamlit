"""Offline packaging contracts. No Snowflake session or deployed-object checks."""
import ast
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tomllib
from zipfile import ZipFile

import pytest
import yaml

from tools import package_handler, prepare_deployment as prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def resources():
    # Test-only names; not defaults or claims about account resources.
    return dict(warehouse="TEST_WH", compute_pool="TEST_POOL", pypi_eai="TEST_EAI",
                owner_role="TEST_APP_OWNER", viewer_role="TEST_APP_VIEWER", connection="Test Alias")


def read_manifest(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["entities"]["employee_roster"]


def test_project_dependencies_and_src_layout():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == "~=3.11.0"
    dependencies = project["project"]["dependencies"]
    assert "streamlit[snowflake]>=1.66,<2" in dependencies
    for name in ("snowflake-connector-python", "snowflake-snowpark-python", "openpyxl", "defusedxml", "regex"):
        assert any(item.split(">", 1)[0].split("<", 1)[0] == name for item in dependencies)
    assert project["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src", "."]
    assert project["tool"]["uv"]["package"] is False
    assert "build-system" not in project
    assert any(item.startswith("PyYAML") for item in project["dependency-groups"]["dev"])


def test_draft_explicitly_incomplete_with_complete_existing_artifacts():
    source = ROOT / "snowflake.yml"
    assert "NOT DEPLOYABLE" in source.read_text(encoding="utf-8")
    entity = read_manifest(source)
    assert entity["identifier"] == dict(name="EMPLOYEE_ROSTER", database="DATASTORE", schema="ROSTER")
    assert entity["runtime_name"] == "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11"
    assert not {"query_warehouse", "compute_pool", "external_access_integrations"} & entity.keys()
    assert entity["artifacts"] == list(prepare.ARTIFACTS)
    assert len(prepare.ARTIFACTS) == len(set(prepare.ARTIFACTS))
    assert all((ROOT / path).is_file() for path in entity["artifacts"])
    production = {path.relative_to(ROOT).as_posix()
                  for directory in (ROOT / "src", ROOT / "app_pages") for path in directory.rglob("*.py")}
    assert production <= set(prepare.ARTIFACTS)


def test_all_local_runtime_imports_and_pages_are_packaged():
    modules = {}
    for relative in prepare.ARTIFACTS:
        path = Path(relative)
        if path.suffix != ".py":
            continue
        parts = list(path.with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    for module, path in modules.items():
        tree = ast.parse((ROOT / path).read_bytes())
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name("." * node.level + target, package)
                targets = [target]
                targets.extend(f"{target}.{alias.name}" for alias in node.names
                               if f"{target}.{alias.name}" in modules)
            for target in targets:
                assert not target.startswith(("dev", "tools", "tests"))
                if target.startswith(("employee_roster", "app_pages")):
                    assert target in modules, (path, target)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("app_pages/"):
                assert node.value in prepare.ARTIFACTS


def test_bundle_is_offline_exact_and_does_not_modify_sources(tmp_path, resources, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Packaging attempted network or command execution")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = {path: (ROOT / path).read_bytes() for path in prepare.ARTIFACTS}
    output = prepare.build(tmp_path / "bundle", **resources)
    entity = read_manifest(output / "snowflake.yml")
    assert entity["query_warehouse"] == resources["warehouse"]
    assert entity["compute_pool"] == resources["compute_pool"]
    assert entity["external_access_integrations"] == [resources["pypi_eai"]]
    assert entity["artifacts"] == list(prepare.ARTIFACTS)
    metadata = json.loads((output / "deployment.json").read_text(encoding="utf-8"))
    assert metadata.items() >= resources.items()
    assert metadata["status"] == "offline-prepared-not-live-verified"
    shipped = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert shipped == set(prepare.ARTIFACTS) | {
        "snowflake.yml", "deployment.json", "grants_owner.sql", "grants_viewer.sql",
    }
    for relative, content in before.items():
        assert (output / relative).read_bytes() == content
        assert (ROOT / relative).read_bytes() == content
    assert all(not path.startswith(("dev/", "tests/", "tools/", "sql/")) for path in shipped)
    assert not any("secrets.toml" in path or path.startswith(".env") for path in shipped)
    # Administrative scripts are local review material, never app artifacts.
    assert not any(path.endswith((".sql", ".json")) for path in entity["artifacts"])


def test_only_explicit_separated_object_grants(tmp_path, resources):
    output = prepare.build(tmp_path / "bundle", **resources)
    owner = (output / "grants_owner.sql").read_text(encoding="utf-8")
    viewer = (output / "grants_viewer.sql").read_text(encoding="utf-8")
    viewer_statements = [line for line in viewer.splitlines() if line.startswith("GRANT ")]
    assert viewer_statements == [
        "GRANT USAGE ON DATABASE DATASTORE TO ROLE TEST_APP_VIEWER;",
        "GRANT USAGE ON SCHEMA DATASTORE.ROSTER TO ROLE TEST_APP_VIEWER;",
        "GRANT USAGE ON STREAMLIT DATASTORE.ROSTER.EMPLOYEE_ROSTER TO ROLE TEST_APP_VIEWER;",
    ]
    assert "ROSTER_EXECUTE(VARCHAR, VARCHAR, VARCHAR) TO ROLE TEST_APP_OWNER;" in owner
    for text in (owner, viewer):
        statements = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
        assert not any(token in statements for token in (
            "ON FUTURE", "ON ALL", "GRANT ROLE", "TO USER", "WITH GRANT OPTION", "OWNERSHIP", "ON TABLE",
        ))


@pytest.mark.parametrize("field", (*prepare.RESOURCE_NAMES, "connection"))
def test_every_resource_is_required_by_api_and_cli(tmp_path, resources, field):
    arguments = resources.copy()
    arguments.pop(field)
    with pytest.raises(TypeError):
        prepare.build(tmp_path / "bundle", **arguments)
    cli = ["--output", str(tmp_path / "bundle")]
    for name, value in arguments.items():
        cli.extend(["--" + name.replace("_", "-"), value])
    with pytest.raises(SystemExit) as exc:
        prepare.main(cli)
    assert exc.value.code == 2
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("field", prepare.RESOURCE_NAMES)
@pytest.mark.parametrize("value", ["", " ", '"quoted"', "DB.NAME", "NAME;DROP", "NAME\n", "NAME--", "1NAME", "N" * 256, "caf\u00e9", "SELECT", "null", "ON"])
def test_unsafe_identifiers_fail_before_output(tmp_path, resources, field, value):
    resources[field] = value
    with pytest.raises(ValueError):
        prepare.build(tmp_path / "bundle", **resources)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("value", ["", " ", " alias", "alias ", "alias\n", "alias;cmd", "$(cmd)", "--prod"])
def test_invalid_connection_rejected(tmp_path, resources, value):
    resources["connection"] = value
    with pytest.raises(ValueError):
        prepare.build(tmp_path / "bundle", **resources)
    assert not (tmp_path / "bundle").exists()


def test_case_normalization_yaml_scalars_and_connection_spaces(tmp_path, resources):
    resources.update(warehouse="no", compute_pool="off", pypi_eai="yes", connection="SNOWFLAKE PROD")
    output = prepare.build(tmp_path / "bundle", **resources)
    entity = read_manifest(output / "snowflake.yml")
    assert entity["query_warehouse"] == "NO"
    assert entity["compute_pool"] == "OFF"
    assert entity["external_access_integrations"] == ["YES"]
    assert json.loads((output / "deployment.json").read_text())["connection"] == "SNOWFLAKE PROD"


@pytest.mark.parametrize("owner,viewer", [("same", "SAME"), ("PUBLIC", "APP_VIEWER"), ("APP_OWNER", "PUBLIC"), ("ACCOUNTADMIN", "APP_VIEWER")])
def test_roles_fail_closed(tmp_path, resources, owner, viewer):
    resources.update(owner_role=owner, viewer_role=viewer)
    with pytest.raises(ValueError):
        prepare.build(tmp_path / "bundle", **resources)
    assert not (tmp_path / "bundle").exists()


def test_refuses_existing_bundle_and_missing_parent(tmp_path, resources):
    output = prepare.build(tmp_path / "bundle", **resources)
    before = (output / "snowflake.yml").read_bytes()
    with pytest.raises(FileExistsError):
        prepare.build(output, **resources)
    assert (output / "snowflake.yml").read_bytes() == before
    with pytest.raises(ValueError):
        prepare.build(tmp_path / "missing" / "bundle", **resources)
    assert not (tmp_path / "missing").exists()
    with pytest.raises(ValueError):
        prepare.build(Path("relative-bundle"), **resources)


@pytest.fixture
def source_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for relative in prepare.ARTIFACTS:
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return source


def test_secrets_and_preview_are_not_collected(tmp_path, resources, source_copy):
    for relative in (".streamlit/secrets.toml", ".env", "dev/preview.py", "tests/test_example.py", "tools/helper.py", "sql/admin.sql"):
        destination = source_copy / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("DO_NOT_SHIP_THIS_SECRET", encoding="utf-8")
    output = prepare.build(tmp_path / "bundle", source_root=source_copy, **resources)
    assert all(b"DO_NOT_SHIP_THIS_SECRET" not in path.read_bytes() for path in output.rglob("*") if path.is_file())


@pytest.mark.parametrize("change", ["missing", "syntax", "unlisted"])
def test_artifact_preflight_is_fail_closed(tmp_path, resources, source_copy, change):
    if change == "missing":
        (source_copy / "app_pages/roster.py").unlink()
    elif change == "syntax":
        (source_copy / "streamlit_app.py").write_text("def broken(:", encoding="utf-8")
    else:
        (source_copy / "src/employee_roster/new_module.py").write_text("", encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        prepare.build(tmp_path / "bundle", source_root=source_copy, **resources)
    assert not (tmp_path / "bundle").exists()


def test_no_source_execution(tmp_path, resources, source_copy):
    (source_copy / "streamlit_app.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    prepare.build(tmp_path / "bundle", source_root=source_copy, **resources)


def test_symlink_artifacts_rejected(tmp_path, resources, source_copy):
    source = source_copy / "streamlit_app.py"
    source.unlink()
    try:
        source.symlink_to(ROOT / "streamlit_app.py")
    except OSError:
        pytest.skip("Symlink creation requires OS permission")
    with pytest.raises(ValueError, match="Symlink"):
        prepare.build(tmp_path / "bundle", source_root=source_copy, **resources)
    assert not (tmp_path / "bundle").exists()


def test_handler_archive_separate_and_deterministic(tmp_path):
    first = package_handler.build(tmp_path / "employee_roster.zip")
    second = package_handler.build(tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert set(archive.namelist()) == {"employee_roster/" + name for name in package_handler.PACKAGE_FILES}
        assert not any(token in name for name in archive.namelist()
                       for token in ("dev/", "preview", "secrets", "app_pages", "ui.py", "excel.py", "tests/"))
    with pytest.raises(FileExistsError):
        package_handler.build(first)


def test_generator_has_no_external_runtime_dependencies():
    tree = ast.parse((ROOT / "tools/prepare_deployment.py").read_bytes())
    allowed = {"__future__", "argparse", "ast", "json", "pathlib", "re", "shutil"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "__import__"}
