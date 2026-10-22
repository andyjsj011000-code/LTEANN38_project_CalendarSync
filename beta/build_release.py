"""Build a deterministic, source-only beta ZIP without runtime secrets or data."""
from hashlib import sha256
from pathlib import Path
import zipfile


def build():
    beta = Path(__file__).resolve().parent
    repo = beta.parent
    source = repo / "web_service"
    allowed_web = ["__init__.py", "api.py", "beta.py", "config.py", "identity.py", "schemas.py", "storage.py", "schema.sql"]
    files = {name: beta / name for name in ("start_beta.py", "start_beta.cmd", "requirements-beta.txt", "README.md")}
    files.update({"web_service/" + name: source / name for name in allowed_web})
    files.update({"web_service/static/" + name: source / "static" / name for name in ("index.html", "beta.css", "beta-calendar.js", "beta.js")})
    files["LICENSE"] = repo / "LICENSE"
    release_dir = beta / "releases"
    release_dir.mkdir(exist_ok=True)
    target = release_dir / "CalendarSync_Beta_0.1.1.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            assert path.is_file() and path.resolve().is_relative_to(repo.resolve())
            assert not any(part in {"var", ".env", "__pycache__", ".test-tmp"} for part in path.parts)
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 22, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            # Batch files need Windows line endings when unzipped on Windows.
            if name.endswith(".cmd"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            archive.writestr(info, data, compresslevel=9)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(files)
    checksum = sha256(target.read_bytes()).hexdigest()
    checksums = ''.join(f"{sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(release_dir.glob('CalendarSync_Beta_*.zip')))
    (release_dir / "SHA256SUMS.txt").write_text(checksums, encoding="ascii")
    print(f"Built {target.name}: {target.stat().st_size} bytes, {len(files)} files, SHA256 {checksum}")
    return target


if __name__ == "__main__":
    build()
