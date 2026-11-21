"""File store for Omen Memory: documents, attachments, past analyses, and backups.

All operations enforce secret rejection and stay local-first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .secret_rejection import validate_no_secrets

logger = logging.getLogger(__name__)


class FileStore:
    """Manages files, documents, and backups associated with memory."""

    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir).resolve()
        self.files_dir = self.root / "files"
        self.backups_dir = self.root / "backups"
        self.exports_dir = self.root / "exports"

        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def save_file(
        self,
        content: str | bytes,
        filename: str,
        *,
        metadata: dict[str, Any] | None = None,
        author: str = "soham.bhagat",
    ) -> dict[str, Any]:
        """Save a file into the store after verifying it contains no secrets."""
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content

        # Check for secrets
        if isinstance(content, str):
            validate_no_secrets(content, metadata)
        else:
            try:
                decoded = raw_bytes.decode("utf-8")
                validate_no_secrets(decoded, metadata)
            except UnicodeDecodeError:
                pass  # Binary content

        safe_name = Path(filename).name
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        file_id = f"file_{sha256[:16]}"
        target_path = self.files_dir / f"{file_id}_{safe_name}"

        # Write atomically
        temp_path = self.files_dir / f".tmp_{file_id}_{safe_name}"
        temp_path.write_bytes(raw_bytes)
        temp_path.replace(target_path)

        # Write sidecar metadata
        meta = {
            "file_id": file_id,
            "filename": safe_name,
            "path": str(target_path),
            "sha256": sha256,
            "size_bytes": len(raw_bytes),
            "author": author,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        meta_path = self.files_dir / f"{file_id}_{safe_name}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return meta

    def read_file(self, file_id_or_path: str) -> bytes:
        """Read file content by file_id or filename."""
        target = self.get_file_path(file_id_or_path)
        if not target or not target.exists():
            raise FileNotFoundError(f"File not found: {file_id_or_path}")
        return target.read_bytes()

    def get_file_path(self, file_id_or_path: str) -> Path | None:
        """Resolve path to stored file."""
        query = str(file_id_or_path)
        # Direct check
        direct = Path(query)
        if direct.exists() and self.files_dir in direct.resolve().parents:
            return direct.resolve()

        # Look in files_dir
        for p in self.files_dir.iterdir():
            if p.is_file() and not p.name.endswith(".meta.json") and not p.name.startswith("."):
                if query in p.name:
                    return p
        return None

    def delete_file(self, file_id_or_path: str) -> bool:
        """Delete stored file and its metadata sidecar."""
        target = self.get_file_path(file_id_or_path)
        if not target or not target.exists():
            return False

        meta_path = Path(str(target) + ".meta.json")
        if meta_path.exists():
            meta_path.unlink()
        target.unlink()
        return True

    def list_files(self) -> list[dict[str, Any]]:
        """List all stored files with metadata."""
        items: list[dict[str, Any]] = []
        for meta_file in sorted(self.files_dir.glob("*.meta.json")):
            try:
                items.append(json.loads(meta_file.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Could not read memory file metadata %s: %s", meta_file.name, exc)
                continue
        return items

    def create_backup_archive(self, db_path: Path, backup_name: str | None = None) -> Path:
        """Create a compressed .tar.gz snapshot of the SQLite database and files."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        name = backup_name or f"omen_memory_backup_{ts}.tar.gz"
        archive_path = self.backups_dir / name
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError("Backup name must be a plain filename")
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.chmod(0o700)
        # Build beside the destination and publish atomically. Exclusive
        # creation prevents a repeated/custom name from silently replacing a
        # known-good backup.
        fd, temporary_name = tempfile.mkstemp(prefix=".omen-backup-", dir=self.backups_dir)
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with tarfile.open(temporary_path, "w:gz") as tar:
                if db_path.exists():
                    tar.add(db_path, arcname=db_path.name)
                if self.files_dir.exists():
                    tar.add(self.files_dir, arcname="files")
            temporary_path.chmod(0o600)
            # link() is an atomic no-overwrite publish on the same filesystem.
            os.link(temporary_path, archive_path)
        except FileExistsError:
            raise FileExistsError(f"Backup already exists: {archive_path}")
        finally:
            temporary_path.unlink(missing_ok=True)

        return archive_path

    def restore_backup_archive(self, archive_path: Path, db_target: Path) -> dict[str, Any]:
        """Extract a backup archive to restore database and files."""
        if not archive_path.exists():
            raise FileNotFoundError(f"Backup archive not found: {archive_path}")

        extracted_files = 0
        db_restored = False
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                parts = PurePosixPath(member.name).parts
                if (not parts or member.name.startswith("/") or "\\" in member.name
                        or any(part in ("", ".", "..") for part in parts)):
                    raise ValueError(f"Unsafe backup member path: {member.name!r}")
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsupported backup member type: {member.name!r}")
                if member.isdir():
                    continue
                if member.name.endswith(".db"):
                    if len(parts) != 1:
                        raise ValueError(f"Database backup must be a top-level file: {member.name!r}")
                    target = db_target
                elif parts[0] == "files" and len(parts) > 1:
                    target = self.root.joinpath(*parts)
                    resolved_root = self.root.resolve()
                    resolved_target = target.resolve(strict=False)
                    if resolved_root not in resolved_target.parents:
                        raise ValueError(f"Backup member escapes restore directory: {member.name!r}")
                else:
                    raise ValueError(f"Unexpected backup member: {member.name!r}")
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError(f"Cannot read backup member: {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                # Reject symlink parents before writing, including symlinks
                # already present in a pre-existing files directory.
                cursor = target.parent
                while cursor != cursor.parent:
                    if cursor.is_symlink():
                        raise ValueError(f"Restore path contains a symlink: {cursor}")
                    if cursor == self.root or cursor == db_target.parent:
                        break
                    cursor = cursor.parent
                fd, temporary_name = tempfile.mkstemp(prefix=".omen-restore-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(temporary_name, 0o600)
                    os.replace(temporary_name, target)
                finally:
                    Path(temporary_name).unlink(missing_ok=True)
                if target == db_target:
                    db_restored = True
                else:
                    extracted_files += 1

        return {"db_restored": db_restored, "files_restored": extracted_files}
