"""NESF v1 binary container for SEM Data Converter and EDXSEM."""

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .payload import FilesystemPayloadSource, canonical_path, check_range, file_hash, safe_path

HEADER = struct.Struct('<8sIHH6q')
MAGIC = b'NESF\r\n\x1a\n'
MAX_JSON_BYTES = 256 * 1024 * 1024


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


@dataclass
class NesfFile:
    path: Path
    manifest: dict
    directory: dict
    payload_offset: int

    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        key = canonical_path(path).casefold()
        entries = [entry for entry in self.directory['payloads'] if entry['path'].casefold() == key]
        if not entries:
            raise FileNotFoundError(path)
        entry = entries[0]
        length = check_range(entry['length'], offset, length)
        if entry['storage'] == 'external':
            return FilesystemPayloadSource(self.path.parent).read(entry['path'], entry['offset'] + offset, length)
        with self.path.open('rb') as stream:
            stream.seek(self.payload_offset + entry['offset'] + offset)
            result = stream.read(length)
        if len(result) != length:
            raise ValueError('Truncated internal payload')
        return result


def read_nesf(path: Path) -> NesfFile:
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        raw = stream.read(HEADER.size)
        if len(raw) != HEADER.size:
            raise ValueError('Truncated NESF header')
        magic, header_size, major, minor, mo, ml, do, dl, po, pl = HEADER.unpack(raw)
        if (magic, header_size, major, minor) != (MAGIC, 64, 1, 0):
            raise ValueError('Unsupported NESF magic/version/header')
        if mo != 64 or ml < 2 or do != mo + ml or dl < 2 or po != do + dl or pl < 0 or po + pl != size:
            raise ValueError('Invalid NESF section ranges')
        if ml > MAX_JSON_BYTES or dl > MAX_JSON_BYTES:
            raise ValueError('NESF JSON section exceeds safety limit')
        stream.seek(mo)
        manifest = json.loads(stream.read(ml).decode('utf-8'))
        directory = json.loads(stream.read(dl).decode('utf-8'))
    if not isinstance(manifest, dict) or manifest.get('format') != 'nesf':
        raise ValueError('Invalid NESF manifest')
    if not isinstance(directory, dict) or not isinstance(directory.get('payloads'), list):
        raise ValueError('Invalid NESF directory')
    paths, ids, internal_ranges = set(), set(), []
    for entry in directory['payloads']:
        if not isinstance(entry, dict):
            raise ValueError('Invalid NESF payload entry')
        key = canonical_path(entry.get('path')).casefold()
        pid = entry.get('payload_id')
        if not isinstance(pid, str) or not pid or key in paths or pid in ids:
            raise ValueError('Payload path/id collision')
        paths.add(key)
        ids.add(pid)
        offset, length = entry.get('offset'), entry.get('length')
        if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
            raise ValueError('Invalid payload range')
        digest = entry.get('sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid payload checksum')
        storage = entry.get('storage')
        if storage == 'internal':
            check_range(pl, offset, length)
            internal_ranges.append((offset, offset + length))
        elif storage != 'external':
            raise ValueError(f'Unknown payload storage: {storage}')
    previous_end = 0
    for start, end in sorted(internal_ranges):
        if start != previous_end:
            raise ValueError('Overlapping or noncontiguous internal payloads')
        previous_end = end
    if previous_end != pl:
        raise ValueError('Unindexed internal payload bytes')
    return NesfFile(path, manifest, directory, po)


def validate_nesf(path: Path, *, verify_external: bool = True, source_root: Path | None = None) -> dict:
    nesf = read_nesf(path)
    for entry in nesf.directory['payloads']:
        if entry['storage'] == 'external':
            if not verify_external:
                continue
            target = safe_path(source_root or nesf.path.parent, entry['path'])
            if target.stat().st_size != entry.get('original_size', entry['offset'] + entry['length']):
                raise ValueError(f"External payload length changed: {entry['path']}")
            digest = file_hash(target, entry['offset'], entry['length'])
        else:
            digest = file_hash(nesf.path, nesf.payload_offset + entry['offset'], entry['length'])
        if digest != entry['sha256']:
            raise ValueError(f"Payload checksum mismatch: {entry['path']}")
    return {'format': 'nesf', 'version': 1, 'payload_count': len(nesf.directory['payloads']),
            'external_verified': verify_external}


def write_nesf(path: Path, manifest: dict, payloads: dict[str, bytes], *,
               external_payloads: list[dict] | None = None, payload_metadata: dict | None = None,
               overwrite: bool = False, source_root: Path | None = None, source_guards: dict | None = None,
               before_publish=None) -> None:
    path = Path(path).resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    entries = [dict(entry) for entry in (external_payloads or [])]
    offset = 0
    blocks = []
    for name, data in sorted(payloads.items()):
        canonical_path(name)
        metadata = dict((payload_metadata or {}).get(name, {}))
        entries.append({**metadata, 'payload_id': metadata.get('payload_id', f'payload:internal:{name}'),
                        'storage': 'internal', 'path': name, 'offset': offset,
                        'length': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        blocks.append(data)
        offset += len(data)
    manifest_bytes = _json_bytes(manifest)
    directory_bytes = _json_bytes({'schema': 'nesf_payload_directory_v1',
                                 'offset_policy': 'internal_offsets_relative_to_internal_payload_area',
                                 'payloads': entries})
    mo, ml = 64, len(manifest_bytes)
    do, dl = mo + ml, len(directory_bytes)
    po = do + dl
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(HEADER.pack(MAGIC, 64, 1, 0, mo, ml, do, dl, po, offset))
            stream.write(manifest_bytes)
            stream.write(directory_bytes)
            for data in blocks:
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        validate_nesf(temporary, source_root=source_root or path.parent)
        for source_path, expected_hash in (source_guards or {}).items():
            if file_hash(Path(source_path)) != expected_hash:
                raise ValueError('Source changed before publication')
        if before_publish is not None:
            before_publish()
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
