"""Windows database bridge; only a shadow copy is upgraded or opened."""

import os
import subprocess
from pathlib import Path

from .payload import file_hash


def find_sqlce() -> Path:
    candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) /
                  'Microsoft SQL Server Compact Edition/v4.0/Desktop/System.Data.SqlServerCe.dll']
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Microsoft SQL CE 4 is required. Specify --sqlce-dll PATH; no Oxford DLL is required.')


def export_oip(source: Path, destination: Path, *, sqlce_dll: Path | None = None) -> Path:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if os.name != 'nt':
        raise OSError('Live database export requires Windows; use convert-export for an existing export.')
    if not source.is_file():
        raise FileNotFoundError(source)
    runtime = Path(sqlce_dll).resolve() if sqlce_dll else find_sqlce()
    if not runtime.is_file():
        raise FileNotFoundError(f'Microsoft SQL CE assembly not found: {runtime}')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Database export destination must be empty')
    before = file_hash(source)
    script = Path(__file__).resolve().parent / 'bridge' / 'read_source.ps1'
    command = [os.environ.get('SystemRoot', 'C:/Windows') + '/System32/WindowsPowerShell/v1.0/powershell.exe',
               '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
               '-Source', str(source), '-Destination', str(destination), '-SqlCeDll', str(runtime)]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if file_hash(source) != before:
        raise ValueError('Original source changed during database export')
    if completed.returncode:
        message = completed.stderr.decode('utf-8', errors='replace')
        raise RuntimeError(f'Database bridge failed: {message}')
    return destination
