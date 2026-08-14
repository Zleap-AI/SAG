from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")


@dataclass(frozen=True, slots=True)
class FileSignature:
    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def from_path(cls, path: Path) -> FileSignature:
        stat = path.stat()
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def to_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "modified_ns": self.modified_ns,
        }


@dataclass(frozen=True, slots=True)
class StoredUpload:
    path: Path
    key: str
    sha256: str
    size_bytes: int
    signature: FileSignature

    def unchanged(self) -> bool:
        try:
            return FileSignature.from_path(self.path) == self.signature
        except FileNotFoundError:
            return False


class OctxStorage:
    def __init__(self, root: Path, *, max_upload_bytes: int) -> None:
        self.root = root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _component(value: str) -> str:
        if value in {"", ".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
            raise ValueError("invalid OCTX path component")
        return value

    def staging_dir(self, transfer_id: str) -> Path:
        return self.root / "staging" / self._component(transfer_id)

    def workspace_dir(self, source_id: str) -> Path:
        return self.root / "workspaces" / self._component(source_id)

    def document_workspace_dir(self, document_id: str) -> Path:
        return self.root / "document-workspaces" / self._component(document_id)

    def resolve_key(self, key: str) -> Path:
        logical = PurePosixPath(key)
        if logical.is_absolute() or not logical.parts or any(
            part in {"", ".", ".."} for part in logical.parts
        ):
            raise ValueError("invalid OCTX artifact key")
        candidate = (self.root / Path(*logical.parts)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("invalid OCTX artifact key")
        return candidate

    def release_key(self, asset_id: str, version: str, package_digest: str) -> str:
        asset = self._component(asset_id)
        release = self._component(version)
        digest = self._component(package_digest)
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("invalid OCTX package digest")
        return f"releases/{asset}/{release}/{digest[7:]}.octx"

    async def stream_upload(
        self, upload: UploadFile, transfer_id: str, *, chunk_size: int = 1024 * 1024
    ) -> StoredUpload:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        directory = self.staging_dir(transfer_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        partial = directory / "input.partial"
        final = directory / "input.octx"
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                while chunk := await upload.read(chunk_size):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ValueError("OCTX upload limit exceeded")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise ValueError("OCTX upload is empty")
            os.replace(partial, final)
            final.chmod(0o400)
            signature = FileSignature.from_path(final)
            return StoredUpload(
                path=final,
                key=f"staging/{self._component(transfer_id)}/input.octx",
                sha256=digest.hexdigest(),
                size_bytes=size,
                signature=signature,
            )
        except BaseException:
            partial.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise

    def publish_release(
        self, temporary: Path, asset_id: str, version: str, package_digest: str
    ) -> str:
        key = self.release_key(asset_id, version, package_digest)
        target = self.resolve_key(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.link(temporary, target)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise FileExistsError(target) from error
            if error.errno != errno.EXDEV:
                raise
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
            try:
                with os.fdopen(descriptor, "wb") as output, temporary.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        target.chmod(0o400)
        return key
