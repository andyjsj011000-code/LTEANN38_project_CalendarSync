"""Source-package boundaries; no subprocesses, installation or network access."""

import ast
import hashlib
import importlib.util
import io
import json
import os
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

spec = importlib.util.spec_from_file_location(
    "web_release_build", Path(__file__).parents[1] / "tools" / "build_web_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in release.REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Safe source: " + name + "\n", encoding="utf-8")
    return root


def contents(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def local_imports(payload):
    tree = ast.parse(payload.decode("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            modules = (
                [node.module.split(".")[0]]
                if node.module
                else [alias.name.split(".")[0] for alias in node.names]
            )
            for module in modules:
                yield "web_service/" + module + ".py"


def test_real_archive_contains_every_local_python_import_and_required_dependencies():
    # Inspect the shipped source without importing app code, opening a database,
    # or depending on a runtime feature flag to exercise an import branch.
    files = contents(release.candidate()[0])
    required = set(release.REQUIRED_FILES)
    for name, payload in files.items():
        if not name.startswith("web_service/") or not name.endswith(".py"):
            continue
        dependencies = set(local_imports(payload))
        assert dependencies <= files.keys(), (name, sorted(dependencies - files.keys()))
        if name in required:
            assert dependencies <= required, (name, sorted(dependencies - required))


def test_real_archive_contains_every_stylesheet_and_script_referenced_by_shipped_html():
    class AssetReferences(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            attribute = (
                "src"
                if tag == "script"
                else "href"
                if tag == "link" and "stylesheet" in attrs.get("rel", "").split()
                else None
            )
            if attribute:
                path = urlsplit(attrs.get(attribute, "")).path
                if path.startswith("/assets/"):
                    self.paths.append("web_service/static/" + path[len("/assets/") :])

    files = contents(release.candidate()[0])
    required = set(release.REQUIRED_FILES)
    for name, payload in files.items():
        if name.startswith("web_service/static/") and name.endswith(".html"):
            references = AssetReferences()
            references.feed(payload.decode("utf-8"))
            assert references.paths, name
            assert set(references.paths) <= files.keys(), name
            assert set(references.paths) <= required, name


def test_rebuild_is_deterministic_and_manifest_verifies_every_file(source):
    first = release.build("releases/web-candidate.zip", root=source)
    original = first.read_bytes()
    for name in release.REQUIRED_FILES:
        os.utime(source / name, (1_600_000_001, 1_700_000_001))
    assert release.build(first, root=source).read_bytes() == original
    files = contents(original)
    manifest = json.loads(files.pop(release.MANIFEST_NAME))
    assert manifest["release_status"] == "review-only"
    assert manifest["ready_for_deployment"] is False
    assert manifest["automatic_deployment"] is False
    assert set(files) == set(release.REQUIRED_FILES)
    for entry in manifest["files"]:
        payload = files[entry["path"]]
        assert entry["bytes"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(item.date_time == (2020, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_sensitive_decoys_and_unknown_modules_never_enter_package(source):
    secret = b"private-student-and-credential-decoy"
    for name in (
        ".env",
        ".env.local",
        "database.sqlite3",
        "server.log",
        "config.json",
        ".ssh/id_rsa",
        "web_service/.env",
        "web_service/private.key",
        "web_service/var/data.sqlite3",
        "web_service/var/tokens.json",
        "web_service/__pycache__/secret.pyc",
        "web_service/credentials.py",
        "web_service/static/student.json",
        "tests_web/test_private.py",
        "app/main.py",
        "deploy/calendarsync-test.service",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
    payload, manifest = release.candidate(source)
    files = contents(payload)
    assert all(secret not in data for data in files.values())
    assert "app/main.py" not in files and "web_service/credentials.py" not in files
    assert (
        files["web_service/render_guard.py"]
        == (source / "web_service/render_guard.py").read_bytes()
    )
    assert "web_service/static/icon-192.png" in manifest["missing_optional"]


def test_known_optional_assets_are_included_and_unknown_assets_are_ignored(source):
    path = source / "web_service/static/icon-192.png"
    path.write_bytes(b"png-test-only-bytes")
    (source / "web_service/static/new-unreviewed.js").write_text("unreviewed")
    payload, manifest = release.candidate(source)
    files = contents(payload)
    assert files["web_service/static/icon-192.png"] == b"png-test-only-bytes"
    assert "web_service/static/new-unreviewed.js" not in files
    assert "web_service/static/icon-192.png" not in manifest["missing_optional"]


def test_reviewed_hosted_factory_and_builder_are_included_without_ci_secrets(source):
    (source / "web_service/hosted.py").write_text('"""Reviewed hosted factory."""\n')
    builder = source / "tools/build_web_release.py"
    builder.parent.mkdir()
    builder.write_bytes(Path(release.__file__).read_bytes())
    workflow = source / ".github/workflows/postgres-beta.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("DATABASE_URL=postgresql://temporary:temporary@localhost/test\n")
    payload, manifest = release.candidate(source)
    files = contents(payload)
    assert files["web_service/hosted.py"] == (source / "web_service/hosted.py").read_bytes()
    assert files["tools/build_web_release.py"] == builder.read_bytes()
    assert ".github/workflows/postgres-beta.yml" not in files
    assert "web_service/hosted.py" not in manifest["missing_optional"]
    assert "tools/build_web_release.py" not in manifest["missing_optional"]


@pytest.mark.parametrize(
    "private",
    [
        b"-----BEGIN PRIVATE KEY-----\nprivate-key-material\n",
        b"WEB_DATA_KEY=private-data-key-value\n",
        b"postgresql://owner:private-password@database.invalid/app",
        b"ghp_" + b"a" * 36,
    ],
)
def test_private_material_in_allowed_file_stops_build_without_echo(source, private):
    (source / "web_service/config.py").write_bytes(private)
    with pytest.raises(ValueError) as error:
        release.build("releases/candidate.zip", root=source)
    assert private.decode() not in str(error.value)
    assert not (source / "releases/candidate.zip").exists()


def test_variable_names_and_explicit_test_placeholders_are_not_secrets(source):
    harmless = (
        b"client_secret = 'test-only-secret'\n"
        b"token = 'synthetic-preview-token'\n"
        b"# WEB_ENTRA_CLIENT_SECRET is supplied through the environment\n"
        b"WEB_DATA_KEY=<generate-locally>\n"
        b"WEB_ENTRA_CLIENT_SECRET=replace-me\n"
    )
    (source / "web_service/config.py").write_bytes(harmless)
    assert contents(release.candidate(source)[0])["web_service/config.py"] == harmless


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_adjacent_empty_secret_assignments_stay_on_their_own_lines(source, newline):
    keys = [b"DATABASE_URL", b"WEB_DATA_KEY", b"WEB_ENTRA_CLIENT_SECRET", b"WEB_OPENAI_API_KEY"]
    harmless = newline.join(key + b"= \t" for key in keys) + newline
    (source / "web_service/config.py").write_bytes(harmless)
    assert contents(release.candidate(source)[0])["web_service/config.py"] == harmless


@pytest.mark.parametrize("live_index", [0, 1, 2, 3])
def test_nonempty_assignment_between_empty_keys_is_still_rejected(source, live_index):
    keys = [b"DATABASE_URL", b"WEB_DATA_KEY", b"WEB_ENTRA_CLIENT_SECRET", b"WEB_OPENAI_API_KEY"]
    lines = [
        key + b"=" + (b"private-value-must-not-be-printed" if i == live_index else b"")
        for i, key in enumerate(keys)
    ]
    (source / "web_service/config.py").write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(ValueError, match="Private assignment") as error:
        release.candidate(source)
    assert "private-value-must-not-be-printed" not in str(error.value)


@pytest.mark.parametrize(
    "target", ["../outside.zip", "web_service/static/download.zip", "plain.txt"]
)
def test_output_cannot_escape_or_replace_source(source, target):
    with pytest.raises(ValueError):
        release.build(target, root=source)


def test_missing_required_source_never_changes_existing_archive(source):
    destination = release.build("candidate.zip", root=source)
    before = destination.read_bytes()
    (source / "web_service/server.py").unlink()
    with pytest.raises(ValueError, match="Required source is missing"):
        release.build(destination, root=source)
    assert destination.read_bytes() == before


def test_atomic_replace_failure_preserves_existing_archive_and_removes_temp(source, monkeypatch):
    destination = release.build("releases/candidate.zip", root=source)
    before = destination.read_bytes()
    (source / "web_service/server.py").write_text("updated safe source")

    def fail_replace(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(release.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replacement"):
        release.build(destination, root=source)
    assert destination.read_bytes() == before
    assert list(destination.parent.glob(".web-release-*.tmp")) == []


@pytest.mark.parametrize("kind", ["file", "directory", "output"])
def test_symlinked_sources_and_destinations_are_rejected(source, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.py").write_text("private-external-source")
    try:
        if kind == "file":
            (source / "web_service/config.py").unlink()
            (source / "web_service/config.py").symlink_to(outside / "config.py")
        elif kind == "directory":
            # An optional path with a linked parent is rejected, rather than
            # followed even if the optional file does not exist outside.
            (source / "deploy").symlink_to(outside, target_is_directory=True)
        else:
            (source / "releases").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This host cannot create test symbolic links")
    with pytest.raises(ValueError, match="Linked"):
        release.build("releases/candidate.zip", root=source)
    assert list(outside.glob("*.zip")) == []


def test_dry_run_reports_archive_hash_without_writing(source, monkeypatch, capsys):
    monkeypatch.setattr(release, "ROOT", source)
    monkeypatch.setattr(
        "sys.argv", ["build_web_release.py", "--output", "releases/candidate.zip", "--dry-run"]
    )
    release.main()
    report = json.loads(capsys.readouterr().out)
    payload, manifest = release.candidate(source)
    assert report["dry_run"] is True
    assert report["archive_sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["manifest"] == manifest
    assert not (source / "releases").exists()
