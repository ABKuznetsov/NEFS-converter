"""Read-only, range-checked access to SEM project payloads."""

import hashlib
from pathlib import Path
from dataclasses import dataclass

BUFFER_SIZE = 1024 * 1024


def canonical_path(path: str) -> str:
    if not isinstance(path, str) or not path or '\\' in path or ':' in path or '\0' in path:
        raise ValueError(f'Unsafe payload path: {path!r}')
    if any(part in ('', '.', '..') for part in path.split('/')):
        raise ValueError(f'Unsafe payload path: {path!r}')
    return path


def safe_path(root: Path, path: str) -> Path:
    target = (root / canonical_path(path)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f'Payload escapes root: {path}')
    return target


def check_range(size: int, offset: int, length: int | None) -> int:
    if not isinstance(offset, int) or offset < 0 or offset > size:
        raise ValueError('Payload offset is outside its range')
    length = size - offset if length is None else length
    if not isinstance(length, int) or length < 0 or length > size - offset:
        raise ValueError('Payload length is outside its range')
    return length


def file_hash(path: Path, offset: int = 0, length: int | None = None) -> str:
    remaining = check_range(path.stat().st_size, offset, length)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        stream.seek(offset)
        while remaining:
            chunk = stream.read(min(BUFFER_SIZE, remaining))
            if not chunk:
                raise ValueError(f'File changed/truncated while hashing: {path}')
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


class FilesystemPayloadSource:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def exists(self, path: str) -> bool:
        return safe_path(self.root, path).is_file()

    def size(self, path: str) -> int:
        return safe_path(self.root, path).stat().st_size

    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        target = safe_path(self.root, path)
        length = check_range(target.stat().st_size, offset, length)
        with target.open('rb') as stream:
            stream.seek(offset)
            result = stream.read(length)
        if len(result) != length:
            raise ValueError(f'Payload changed while reading: {path}')
        return result

    def open(self, path: str):
        return safe_path(self.root, path).open('rb')

    def sha256(self, path: str) -> str:
        return file_hash(safe_path(self.root, path))


@dataclass
class SourceSnapshot:
    root: Path
    entries: dict[str, dict]

    @staticmethod
    def inventory(root: Path) -> dict[str, Path]:
        directory = root / 'data'
        if not directory.is_dir():
            raise FileNotFoundError('Source data/ directory is missing')
        result = {}
        for path in directory.iterdir():
            if path.is_file() and path.suffix.casefold() == '.dat':
                relative = canonical_path(f'data/{path.name}')
                key = relative.casefold()
                if key in result:
                    raise ValueError('External payload path collision')
                result[key] = safe_path(root, relative)
        return result

    @classmethod
    def capture(cls, root: Path):
        root = Path(root).resolve()
        entries = {}
        for key, path in cls.inventory(root).items():
            before = path.stat()
            digest = file_hash(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f'Source payload changed while hashing: {path.name}')
            entries[key] = {'path': f'data/{path.name}', 'length': before.st_size, 'sha256': digest}
        return cls(root, entries)

    def verify(self) -> None:
        inventory = self.inventory(self.root)
        if set(inventory) != set(self.entries):
            raise ValueError('Source data inventory changed during conversion')
        for key, path in inventory.items():
            if path.stat().st_size != self.entries[key]['length'] or file_hash(path) != self.entries[key]['sha256']:
                raise ValueError(f'Source payload changed during conversion: {path.name}')
