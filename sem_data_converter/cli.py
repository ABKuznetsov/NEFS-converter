"""Command-line interface for the separately distributed SEM Data Converter."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .export_reader import load_export
from .model import build_project
from .nesf import validate_nesf, write_nesf
from .payload import file_hash, SourceSnapshot
from .sqlce import export_oip


def _output_path(root: Path, output: Path) -> Path:
    root, output = root.resolve(), output.resolve()
    if output.parent != root or output.suffix.casefold() != '.nesf':
        raise ValueError('NESF output must be beside the source data directory, not inside data/.')
    return output


def convert_export(export_root: Path, source_root: Path, output: Path, *, overwrite=False, source_guard=None,
                   source_snapshot=None, progress=None) -> dict:
    source_root = source_root.resolve()
    output = _output_path(source_root, output)
    if output.exists() and not overwrite:
        raise FileExistsError(f'Output already exists: {output}. Use --overwrite explicitly.')
    if not (source_root / 'data').is_dir():
        raise FileNotFoundError('The source data/ directory is missing.')
    log = progress or (lambda message: print(message, file=sys.stderr))
    log('Reading source tables and linking measurements...')
    snapshot = source_snapshot or SourceSnapshot.capture(source_root)
    dataset = build_project(load_export(export_root), source_root, source_snapshot=snapshot)
    if source_guard and file_hash(source_guard[0]) != source_guard[1]:
        raise ValueError('Source database changed during conversion')
    log('Writing and validating NESF...')
    write_nesf(output, dataset.manifest, dataset.internal, external_payloads=dataset.external,
               payload_metadata=dataset.payload_metadata, overwrite=overwrite,
               source_guards={source_guard[0]: source_guard[1]} if source_guard else None,
               before_publish=snapshot.verify)
    return {'generator': 'SEM Data Converter', 'status': 'partial' if dataset.diagnostics else 'converted',
            'output': str(output), 'needs_visual_validation': True,
            'counts': {key: len(value) for key, value in dataset.manifest['fast_index'].items() if isinstance(value, list)},
            'original_dat_files': len(dataset.external), 'internal_spectra': len(dataset.internal),
            'diagnostics': dataset.diagnostics}


def convert_source(source: Path, output: Path | None = None, *, sqlce_dll=None, overwrite=False, progress=None) -> dict:
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() != '.oip':
        raise ValueError('Unsupported source dataset type for this demonstration build.')
    output = _output_path(source.parent, output or source.with_suffix('.nesf'))
    if output.exists() and not overwrite:
        raise FileExistsError(f'Output already exists: {output}. Use --overwrite explicitly.')
    log = progress or (lambda message: print(message, file=sys.stderr))
    log('Checking original SEM payloads...')
    before = file_hash(source)
    snapshot = SourceSnapshot.capture(source.parent)
    log('Extracting a read-only working copy...')
    with tempfile.TemporaryDirectory(prefix='sem-data-converter-') as temporary:
        exported = export_oip(source, Path(temporary), sqlce_dll=sqlce_dll)
        return convert_export(exported, source.parent, output, overwrite=overwrite,
                              source_guard=(source, before), source_snapshot=snapshot, progress=log)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='sem-data-converter', description='SEM Data Converter: source measurements to NESF.')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('gui', help='Open the file-selection window')
    convert = commands.add_parser('convert', help='Convert a source SEM dataset to NESF')
    convert.add_argument('source', type=Path)
    convert.add_argument('--sqlce-dll', type=Path, help='Path to Microsoft SQL CE 4 assembly')
    convert.add_argument('--output', type=Path, help='NESF file beside the source (default: source name with .nesf)')
    convert.add_argument('--overwrite', action='store_true')
    exported = commands.add_parser('convert-export', help='Convert an already extracted database export')
    exported.add_argument('export_root', type=Path)
    exported.add_argument('--source-root', type=Path, required=True)
    exported.add_argument('--output', type=Path, required=True)
    exported.add_argument('--overwrite', action='store_true')
    validate = commands.add_parser('validate', help='Validate NESF structure and original payload checksums')
    validate.add_argument('source', type=Path)
    return root


def main(argv=None) -> int:
    if argv == [] or (argv is None and len(sys.argv) == 1):
        from .gui import main_gui
        return main_gui()
    args = parser().parse_args(argv)
    try:
        if args.command == 'gui':
            from .gui import main_gui
            return main_gui()
        elif args.command == 'validate':
            summary = validate_nesf(args.source)
        elif args.command == 'convert-export':
            summary = convert_export(args.export_root, args.source_root, args.output, overwrite=args.overwrite)
        else:
            summary = convert_source(args.source, args.output, sqlce_dll=args.sqlce_dll, overwrite=args.overwrite)
        print(json.dumps(summary, ensure_ascii=True, allow_nan=False, indent=2))
        return 3 if summary.get('status') == 'partial' else 0
    except KeyboardInterrupt:
        print('SEM Data Converter: cancelled; original data is unchanged.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'SEM Data Converter: {exc}', file=sys.stderr)
        return 2
