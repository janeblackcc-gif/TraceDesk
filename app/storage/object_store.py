from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import BinaryIO, Iterable

KEY = re.compile(r'sha256/([0-9a-f]{2})/([0-9a-f]{64})\Z')


class StorageError(ValueError):
    pass


def comparable_path(path: PurePath) -> PurePath:
    """Normalize equivalent Win32 namespace prefixes after resolving reparse points.

    ntpath.realpath can retain the extended prefix when another thread publishes
    a file between its existence probes. PureWindowsPath treats the two spellings
    as different anchors even though they name the same local/UNC location.
    """
    if isinstance(path, PureWindowsPath):
        value = str(path)
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif re.match(r'^\\\\\?\\[A-Za-z]:\\', value):
            value = value[4:]
        return PureWindowsPath(value)
    return path


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size_bytes: int
    storage_key: str


class ObjectStore:
    def __init__(self, root: Path):
        if root.is_symlink():
            raise StorageError('Object root must not be a symlink')
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.temporary = self.root / '.incoming'
        if self.temporary.is_symlink():
            raise StorageError('Temporary object directory must not be a symlink')
        self.temporary.mkdir(exist_ok=True)

    def path_for(self, key: str) -> Path:
        match = KEY.fullmatch(key)
        if not match or match[1] != match[2][:2]:
            raise StorageError('Invalid object key')
        path = self.root / key
        if any(part.is_symlink() for part in [self.root / 'sha256', path.parent, path]):
            raise StorageError('Object path must not contain symlinks')
        if not comparable_path(path.resolve()).is_relative_to(comparable_path(self.root)):
            raise StorageError('Object outside storage root')
        return path

    def put(self, source: BinaryIO, *, max_bytes: int = 20 * 1024 * 1024) -> StoredObject:
        if max_bytes < 0:
            raise ValueError('max_bytes must be nonnegative')
        fd, name = tempfile.mkstemp(prefix='upload-', dir=self.temporary)
        temporary = Path(name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, 'wb') as destination:
                while data := source.read(min(1024 * 1024, max_bytes - size + 1)):
                    size += len(data)
                    if size > max_bytes:
                        raise StorageError('OBJECT_TOO_LARGE')
                    digest.update(data)
                    destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            checksum = digest.hexdigest()
            key = f'sha256/{checksum[:2]}/{checksum}'
            destination_path = self.path_for(key)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            # Hard-link publication is atomic and never replaces an existing object,
            # including on POSIX where rename would silently replace it.
            try:
                os.link(temporary, destination_path)
            except FileExistsError:
                self.verify(StoredObject(checksum, size, key))
            if sys.platform != 'win32':
                directory_fd = os.open(destination_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return StoredObject(checksum, size, key)
        finally:
            # This is an operation-owned scratch file, never a user document.
            temporary.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        return self.path_for(key).open('rb')

    def recycle(self, key: str) -> None:
        """Move an authorized, unreferenced object to the OS trash; never unlink it.

        The GC service holds the digest lock and checks references before calling.
        Missing files are safe on retry after a filesystem success / DB rollback.
        """
        from send2trash import send2trash
        path = self.path_for(key)
        if path.exists():
            send2trash(str(path))

    def verify(self, item: StoredObject) -> None:
        path = self.path_for(item.storage_key)
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != item.sha256 or path.stat().st_size != item.size_bytes:
            raise StorageError('OBJECT_CORRUPT')

    def orphan_candidates(self, referenced_keys: Iterable[str], *, minimum_age_seconds: int = 86400) -> list[str]:
        """Report unreferenced objects; deletion requires the retention/GC service."""
        if minimum_age_seconds < 0:
            raise ValueError('minimum_age_seconds must be nonnegative')
        referenced = set(referenced_keys)
        cutoff = time.time() - minimum_age_seconds
        candidates = []
        for path in (self.root / 'sha256').glob('*/*'):
            key = path.relative_to(self.root).as_posix()
            safe = self.path_for(key)
            if key not in referenced and safe.is_file() and safe.stat().st_mtime <= cutoff:
                candidates.append(key)
        return sorted(candidates)
