"""Build a review-only web source ZIP, without running application code.

The package uses closed file lists, never a recursive source-directory glob.
No installation, credentials, database initialization or deployment occurs.
The source tree may be an existing repository copy; outputs must stay inside
that tree. This is a source candidate, not evidence of production readiness.
"""

import argparse
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "WEB_RELEASE_MANIFEST.json"
REQUIRED_FILES = (
    "requirements-web.txt",
    "requirements-postgres-experimental.txt",
    "NOTIFICATION_WORK_IN_PROGRESS.md",
    "docs/BETA_PREPARATION_STATUS_KO.md",
    "docs/TEST_SERVER_PRIVACY.md",
    "docs/TEAMS_ASSIGNMENTS.md",
    "web_service/__init__.py",
    "web_service/__main__.py",
    "web_service/api.py",
    "web_service/assignments.py",
    "web_service/beta.py",
    "web_service/calendar_rules.py",
    "web_service/collection_types.py",
    "web_service/composition.py",
    "web_service/config.py",
    "web_service/connections.py",
    "web_service/direct_source.py",
    "web_service/graph_subscriptions.py",
    "web_service/identity.py",
    "web_service/personal_routes.py",
    "web_service/personal_types.py",
    "web_service/personal_ports.py",
    "web_service/request_dependencies.py",
    "web_service/settings_routes.py",
    "web_service/notification_fanout.py",
    "web_service/notification_queue.py",
    "web_service/notification_runtime.py",
    "web_service/notification_target.py",
    "web_service/notification_webhook.py",
    "web_service/notification_worker.py",
    "web_service/ops.py",
    "web_service/oracle_source.py",
    "web_service/planner_drafts.py",
    "web_service/planner_schema.py",
    "web_service/planner_trash.py",
    "web_service/ports.py",
    "web_service/postgres_auth_store.py",
    "web_service/postgres_channel_ingestion.py",
    "web_service/postgres_channels.py",
    "web_service/postgres_connection.py",
    "web_service/postgres_database.py",
    "web_service/postgres_devices.py",
    "web_service/postgres_events.py",
    "web_service/postgres_ingestion_status.py",
    "web_service/postgres_notification_queue.py",
    "web_service/postgres_operations.py",
    "web_service/postgres_personal_tasks.py",
    "web_service/postgres_preferences.py",
    "web_service/postgres_push.py",
    "web_service/postgres_retention.py",
    "web_service/postgres_shared_schema.py",
    "web_service/postgres_source_cache.py",
    "web_service/postgres_sync.py",
    "web_service/postgresql_schema_draft.sql",
    "web_service/preview.py",
    "web_service/push.py",
    "web_service/render_guard.py",
    "web_service/schema.sql",
    "web_service/schemas.py",
    "web_service/school.py",
    "web_service/school_graph.py",
    "web_service/school_security.py",
    "web_service/school_storage.py",
    "web_service/server.py",
    "web_service/shared_collection.py",
    "web_service/source_updates.py",
    "web_service/sqlite_ingestion_status.py",
    "web_service/storage.py",
    "web_service/subscription_registry.py",
    "web_service/test_storage.py",
    "web_service/static/beta-assignments.js",
    "web_service/static/beta-calendar.js",
    "web_service/static/beta-list-controls.js",
    "web_service/static/beta-task-drafts.js",
    "web_service/static/beta.css",
    "web_service/static/beta.js",
    "web_service/static/icon.svg",
    "web_service/static/index.html",
    "web_service/static/manifest.webmanifest",
    "web_service/static/school.html",
    "web_service/static/service-worker.js",
)
# Known companion files only. Missing binary icons are disclosed, not invented.
OPTIONAL_FILES = (
    "tools/build_web_release.py",
    "web_service/hosted.py",
    "web_service/postgres_bootstrap.py",
    "web_service/static/icon-192.png",
    "web_service/static/icon-512.png",
    "deploy/render-beta.yaml.example",
    "deploy/render-beta.env.example",
    "docs/RENDER_BETA_DRAFT.md",
)
_MAX_FILE_BYTES = 8 * 1024 * 1024
_ENV_SECRET = re.compile(
    rb"^[ \t]*(?:WEB_(?:DATA_KEY|VAPID_PRIVATE_KEY|ENTRA_CLIENT_SECRET|OPENAI_API_KEY)|"
    rb"DATABASE_URL)[ \t]*=[ \t]*([^\r\n]*)",
    re.MULTILINE,
)
_PRIVATE_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"\b(?:postgresql|postgres|oracle)://[^\s/:]+:[^\s/@]+@"),
)


def _reparse(metadata):
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _root(path):
    path = Path(path).absolute()
    metadata = path.lstat()
    if _reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Source root must be a regular directory")
    return path.resolve()


def _check_chain(root, relative, *, missing=False):
    current = root
    parts = Path(relative).parts
    if not parts or any(part in {".", ".."} for part in parts) or Path(relative).is_absolute():
        raise ValueError("Invalid source path")
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise ValueError("Required source is missing: " + str(relative)) from None
        if _reparse(metadata):
            raise ValueError("Linked source paths are not allowed: " + str(relative))
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Source parent must be a directory: " + str(relative))
    return current, metadata


def _placeholder(value):
    value = value.strip().strip(b"\"'")
    lowered = value.lower()
    return (
        not value
        or lowered in {b"replace-me", b"change-me", b"placeholder", b"example"}
        or lowered.startswith((b"test-only-", b"synthetic-", b"${", b"<"))
    )


def _check_private_material(name, payload):
    if any(pattern.search(payload) for pattern in _PRIVATE_PATTERNS):
        raise ValueError("Private material detected in allowed source: " + name)
    if any(not _placeholder(match[1]) for match in _ENV_SECRET.finditer(payload)):
        raise ValueError("Private assignment detected in allowed source: " + name)


def _read_source(root, name, *, optional=False):
    checked = _check_chain(root, name, missing=optional)
    if checked is None:
        return None
    path, expected = checked
    if not stat.S_ISREG(expected.st_mode) or expected.st_size > _MAX_FILE_BYTES:
        raise ValueError("Allowed source must be a bounded regular file: " + name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (
            _reparse(actual)
            or not stat.S_ISREG(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValueError("Source identity changed: " + name)
        # Check parent reparse points again after opening. On systems supporting
        # O_NOFOLLOW the final component is also protected by the open itself.
        after = _check_chain(root, name)[1]
        if (actual.st_dev, actual.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("Source identity changed: " + name)
        payload = stream.read(_MAX_FILE_BYTES + 1)
    if len(payload) > _MAX_FILE_BYTES:
        raise ValueError("Allowed source exceeded the size bound: " + name)
    _check_private_material(name, payload)
    return payload


def candidate(root=ROOT):
    """Return deterministic ZIP bytes and its content-only review manifest."""
    root = _root(root)
    payloads = {name: _read_source(root, name) for name in REQUIRED_FILES}
    absent = []
    for name in OPTIONAL_FILES:
        payload = _read_source(root, name, optional=True)
        if payload is None:
            absent.append(name)
        else:
            payloads[name] = payload
    manifest = {
        "format": "calendarsync-web-source-v1",
        "release_status": "review-only",
        "ready_for_deployment": False,
        "automatic_deployment": False,
        "missing_optional": sorted(absent),
        "files": [
            {"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in sorted(payloads.items())
        ],
    }
    payloads[MANIFEST_NAME] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in sorted(payloads.items()):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, payload, compresslevel=9)
    return stream.getvalue(), manifest


def _destination(root, output):
    output = Path(output)
    if not output.is_absolute():
        output = root / output
    if ".." in output.parts:
        raise ValueError("Output must stay inside the source workspace")
    output = output.absolute()
    try:
        relative = output.relative_to(root)
    except ValueError:
        raise ValueError("Output must stay inside the source workspace") from None
    if (
        output.suffix.lower() != ".zip"
        or not relative.parts
        or any(part.startswith(".") or ":" in part for part in relative.parts)
        or relative.parts[0] in {"web_service", "docs", "deploy", "tools", "tests_web"}
    ):
        raise ValueError("Choose a ZIP output outside the source directories")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if _reparse(metadata):
            raise ValueError("Linked output paths are not allowed")
        if current != output and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Output parent must be a directory")
        if current == output and not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Output must be a regular ZIP file")
    return output


def build(output, *, root=ROOT):
    """Atomically write a deterministic candidate to an explicit local ZIP path."""
    root = _root(root)
    destination = _destination(root, output)
    payload, _ = candidate(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir, before creating a sibling temporary file.
    _destination(root, destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".web-release-", suffix=".tmp", dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _destination(root, destination)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description="Build a review-only CalendarSync web source ZIP")
    parser.add_argument(
        "--output", type=Path, required=True, help="Explicit ZIP path inside this source workspace"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print manifest and archive hash without writing"
    )
    args = parser.parse_args()
    root = _root(ROOT)
    destination = _destination(root, args.output)
    payload, manifest = candidate(root)
    if not args.dry_run:
        written = build(destination, root=root)
        # Build re-reads the source: report the actual bytes if another editor
        # changed a file after candidate() completed.
        payload = written.read_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME))
    report = {
        "output": destination.relative_to(root).as_posix(),
        "dry_run": args.dry_run,
        "archive_bytes": len(payload),
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "manifest": manifest,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
