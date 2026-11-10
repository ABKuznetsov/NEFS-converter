"""Load the JSON/BIN/XML export produced by the database bridge."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .payload import safe_path


@dataclass
class ExportedProject:
    tables: dict[str, list[dict]]
    xml: dict[tuple[str, int, str], str] = field(default_factory=dict)
    schema: list[dict] = field(default_factory=list)
    source_name: str = 'source.oip'


def load_export(root: Path) -> ExportedProject:
    root = Path(root).resolve()
    metadata = json.loads((root / 'export.json').read_text(encoding='utf-8-sig'))
    if not isinstance(metadata, dict) or not isinstance(metadata.get('tables'), list):
        raise ValueError('Invalid database export manifest')
    result = ExportedProject({}, schema=metadata.get('schema', []), source_name=metadata.get('source_name', 'source.oip'))
    seen = set()
    for table in metadata['tables']:
        name = table.get('name')
        if not isinstance(name, str) or not name or name.casefold() in seen:
            raise ValueError('Invalid or duplicate exported table')
        seen.add(name.casefold())
        rows = json.loads(safe_path(root, table['file']).read_text(encoding='utf-8-sig'))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f'Table rows must be an array of objects: {name}')
        for index, row in enumerate(rows):
            for column, value in list(row.items()):
                if isinstance(value, dict) and 'file' in value:
                    raw = safe_path(root, value['file']).read_bytes()
                    if len(raw) != value.get('bytes'):
                        raise ValueError(f'Exported binary size mismatch: {name}.{column}')
                    row[column] = raw
                    if value.get('xml'):
                        result.xml[name, index, column] = safe_path(root, value['xml']).read_text(encoding='utf-8-sig')
        result.tables[name] = rows
    return result
