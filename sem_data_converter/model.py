"""Build current NESF indexes and a lossless database snapshot."""

import base64
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .decoders import (Reader, UnsupportedLayout, blobs, decode_spectrum, image_layout,
                       local_name, matching_blob, quant_values, tile_layout, validate_tile_envelope, xml_root)
from .export_reader import ExportedProject
from .metadata import acquisition_conditions, image_scale, region_position
from .payload import FilesystemPayloadSource, SourceSnapshot

NIL = '00000000-0000-0000-0000-000000000000'
TABLE_KEYS = {'samples': ('Samples', 'SampleId'), 'sites': ('Analyses', 'AnalysisId'),
              'images': ('ElectronImages', 'ImageId'), 'spectra': ('EDXSpectra', 'SpectrumId'),
              'maps': ('XrayMapImages', 'ImageId'), 'quant': ('QuantResults', 'QuantResultId')}


@dataclass
class ProjectDataset:
    manifest: dict
    internal: dict[str, bytes]
    external: list[dict]
    payload_metadata: dict
    diagnostics: list[dict]
    source_snapshot: SourceSnapshot


def _snapshot(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode('ascii')
    if isinstance(value, dict):
        return {k: _snapshot(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snapshot(v) for v in value]
    return value


def build_project(exported: ExportedProject, source_root: Path, *, source_snapshot: SourceSnapshot | None = None) -> ProjectDataset:
    source_root = Path(source_root).resolve()
    source = FilesystemPayloadSource(source_root)
    snapshot = source_snapshot or SourceSnapshot.capture(source_root)
    if snapshot.root != source_root:
        raise ValueError('Source snapshot belongs to another dataset')
    tables = exported.tables
    raw_nodes = tables.get('DataNodes', [])
    by_node, by_assoc, children = {}, {}, {}
    for node in raw_nodes:
        ident = str(node.get('Id', '')).lower()
        if not ident or ident in by_node:
            raise ValueError('Missing or duplicate DataNode identity')
        by_node[ident] = node
        by_assoc.setdefault(str(node.get('AssociatedId', '')).lower(), []).append(node)
        children.setdefault(str(node.get('ParentId', '')).lower(), []).append(node)

    def ancestry(node):
        trail, visited = [], set()
        while node:
            ident = str(node['Id']).lower()
            if ident in visited:
                raise ValueError('Cycle in DataNodes graph')
            visited.add(ident)
            trail.append(node)
            parent = str(node.get('ParentId') or '').lower()
            if parent not in ('', NIL) and parent not in by_node:
                raise ValueError(f'Broken DataNode parent: {parent}')
            node = by_node.get(parent)
        return trail

    for node in raw_nodes:
        ancestry(node)
    labels, rows_by_id, xml_by_id = {}, {}, {}
    for section, (table, key) in TABLE_KEYS.items():
        labels[section] = {}
        for index, row in enumerate(tables.get(table, [])):
            ident = str(row.get(key, '')).lower()
            if not ident or (table, ident) in rows_by_id:
                raise ValueError(f'Missing or duplicate identity in {table}')
            rows_by_id[table, ident] = row
            labels[section][ident] = str(row.get('Label') or ident)
            xml_by_id[table, ident] = exported.xml.get((table, index, 'ObjectBlob'), '')

    supported_types = {1: ('Project', 'ProjectId'), 2: ('EDXSpectra', 'SpectrumId'),
                       3: ('ElectronImages', 'ImageId'), 4: ('Samples', 'SampleId'),
                       5: ('SmartMaps', 'SmartMapId'), 7: ('XrayMapImages', 'ImageId'),
                       9: ('LayerMaps', 'LayerMapId'),
                       11: ('Analyses', 'AnalysisId'), 13: ('AreaOfInterests', 'AreaOfInterestId'),
                       15: ('QuantResults', 'QuantResultId')}
    for node in raw_nodes:
        target = supported_types.get(node.get('AssociatedType'))
        if target:
            table, key = target
            ident = str(node.get('AssociatedId', '')).lower()
            if not any(str(row.get(key, '')).lower() == ident for row in tables.get(table, [])):
                raise ValueError(f'Broken object association: {node["Id"]} -> {table}/{ident}')
    smartmap_ids = {str(row['SmartMapId']).lower() for row in tables.get('SmartMaps', [])}
    for row in tables.get('TileGroups', []):
        if str(row.get('SmartMapId', '')).lower() not in smartmap_ids:
            raise ValueError('TileGroup references an absent SmartMap')
    for table, key in (('AreaOfInterests', 'AreaOfInterestId'), ('SmartMaps', 'SmartMapId')):
        for index, row in enumerate(tables.get(table, [])):
            ident = str(row[key]).lower()
            if (table, ident) in rows_by_id:
                raise ValueError(f'Duplicate identity in {table}')
            rows_by_id[table, ident] = row
            xml_by_id[table, ident] = exported.xml.get((table, index, 'ObjectBlob'), '')

    def context(node):
        ids = [str(n.get('AssociatedId', '')).lower() for n in ancestry(node)]
        return {'sample_id': next((v for v in ids if v in labels['samples']), ''),
                'site_id': next((v for v in ids if v in labels['sites']), '')}

    def object_context(ident):
        nodes = by_assoc.get(ident, [])
        return context(nodes[0]) if nodes else {'sample_id': '', 'site_id': ''}

    diagnostics, external, internal, internal_meta = [], [], {}, {}
    fast = {'schema': 'nesf_fast_index_v1', 'electron_images': [], 'xray_maps': [],
            'spectra': [], 'composition': [], 'smartmaps': []}
    referenced = set()

    def warn(kind, ident, reason):
        diagnostics.append({'kind': kind, 'object_id': ident, 'status': 'preserved_opaque', 'reason': str(reason)})

    def dat_ref(ident, suffix=''):
        path = f'data/{ident}{suffix}.dat'
        if not source.exists(path):
            raise FileNotFoundError(f'Missing referenced payload: {path}')
        referenced.add(path.casefold())
        return {'payload_id': f'payload:external:{ident}{suffix}', 'payload_path': path}

    images = {}
    for table, section in (('ElectronImages', 'electron_images'), ('XrayMapImages', 'xray_maps')):
        for row in tables.get(table, []):
            ident = str(row['ImageId']).lower()
            item = {'image_id': ident, 'label': row.get('Label', ident), **object_context(ident), **dat_ref(ident)}
            xml = xml_by_id[table, ident]
            try:
                if not xml:
                    raise UnsupportedLayout('No decoded image XML')
                layout = image_layout(xml)
                if source.size(item['payload_path']) != layout['total_bytes']:
                    raise ValueError(f'Image payload length does not match pyramid: {ident}')
                first = layout['levels'][0]
                item.update(width_px=first['width'], height_px=first['height'], dtype=layout['dtype'],
                            payload_offset=first['byte_offset'], payload_length=first['byte_length'],
                            pyramid=layout['levels'], short_signedness=layout['short_signedness'])
                if table == 'ElectronImages':
                    direct = blobs(xml_root(xml))
                    role = 'unknown'
                    for blob in direct:
                        if blob.endswith(b'\0\x03BSE'): role = 'bse'
                        elif blob.endswith(b'\0\x02SE'): role = 'se'
                    item['image_role'] = role
                    if direct and len(direct[0]) >= 20:
                        aid = str(uuid.UUID(bytes_le=direct[0][4:20]))
                        aoi_xml = xml_by_id.get(('AreaOfInterests', aid), '')
                        if aoi_xml:
                            item['scale'] = image_scale(aoi_xml, first['width'], first['height'])
                            item['acquisition_context'] = acquisition_conditions(aoi_xml)
                    if not item.get('scale'):
                        warn('scale', ident, 'No native metric scale interpretation available')
            except UnsupportedLayout as exc:
                warn('image', ident, exc)
            fast[section].append(item)
            images[ident] = item

    spectrum_data = {}
    for row in tables.get('EDXSpectra', []):
        ident = str(row['SpectrumId']).lower()
        try:
            xml = xml_by_id['EDXSpectra', ident]
            if not xml:
                raise UnsupportedLayout('No decoded spectrum XML')
            decoded = decode_spectrum(matching_blob(xml, ident), ident, row.get('Label', ''),
                                      expected_instance=row['InstanceVersion'])
            path = f'payloads/spectra/{ident}.raw.u32'
            internal[path] = decoded.pop('count_bytes')
            pid = f'payload:spectrum:{ident}:raw_counts'
            internal_meta[path] = {'payload_id': pid, 'dtype': 'uint32_le', 'shape': [decoded['channel_count']],
                                   'channel_count': decoded['channel_count'], 'role': 'spectrum_raw_counts'}
            spectrum_data[ident] = {**decoded, 'raw_counts_payload': internal_meta[path]}
        except UnsupportedLayout as exc:
            warn('spectrum', ident, exc)

    quant_by_id = {}
    for row in tables.get('QuantResults', []):
        ident = str(row['QuantResultId']).lower()
        try:
            xml = xml_by_id['QuantResults', ident]
            quant_by_id[ident] = quant_values(xml) if xml else []
            if not xml:
                warn('composition', ident, 'No decoded quant XML')
            elif not quant_by_id[ident]:
                root = xml_root(xml)
                empty_results = next((e for e in root if local_name(e) == 'xppResults' and len(e) == 0), None)
                if local_name(root) != 'QuantResultEntity' or empty_results is None:
                    warn('composition', ident, 'No recognized quant results; normalized composition unavailable')
        except UnsupportedLayout as exc:
            warn('composition', ident, exc)
            quant_by_id[ident] = []

    for node in raw_nodes:
        ident = str(node.get('AssociatedId', '')).lower()
        if ident not in labels['spectra']:
            continue
        item = {'spectrum_id': ident, 'spectrum_instance_id': node['Id'], 'node_id': node['Id'],
                'label': labels['spectra'][ident], 'source_table': 'EDXSpectra', **context(node)}
        decoded = spectrum_data.get(ident)
        if decoded:
            item.update(channel_count=decoded['channel_count'], raw_counts_payload=decoded['raw_counts_payload'],
                        total_counts=decoded['total_counts'], energy_calibration={
                            'offset_keV': decoded['offset_eV'] / 1000, 'gain_keV': decoded['gain_eV'] / 1000,
                            'source': 'stored_spectrum_gain_offset',
                            'unit_interpretation': 'eV_values_as_in_upstream_requires_external_validation'})
        trail = ancestry(node)
        image_node = next((n for n in trail if str(n.get('AssociatedId', '')).lower() in labels['images']), None)
        area_node = next((n for n in trail if ('AreaOfInterests', str(n.get('AssociatedId', '')).lower()) in rows_by_id), None)
        if area_node is None:
            areas = [n for n in children.get(str(node['Id']).lower(), [])
                     if ('AreaOfInterests', str(n.get('AssociatedId', '')).lower()) in rows_by_id]
            if len(areas) > 1:
                raise ValueError('Ambiguous spectrum AOI relationship')
            area_node = areas[0] if areas else None
        if image_node is None and item['site_id']:
            candidates = [im for im in fast['electron_images'] if im['site_id'] == item['site_id']]
            if len(candidates) == 2 and {im.get('image_role') for im in candidates} == {'bse', 'se'}:
                def frame(im):
                    return (im.get('width_px'), im.get('height_px'), im.get('scale'), im.get('acquisition_context'))
                if frame(candidates[0]) == frame(candidates[1]):
                    candidates = [im for im in candidates if im['image_role'] == 'bse']
            if len(candidates) == 1:
                image_node = by_assoc[candidates[0]['image_id']][0]
                item['image_relation_source'] = 'unique_site_image_or_matching_bse_se_frame'
            elif candidates and area_node is not None:
                warn('position', node['Id'], 'Ambiguous site image frame; explicit registration required')
        item['role'] = 'unpositioned'
        if image_node is not None and area_node is not None:
            iid, aid = str(image_node['AssociatedId']).lower(), str(area_node['AssociatedId']).lower()
            image, xml = images[iid], xml_by_id.get(('AreaOfInterests', aid), '')
            if xml and image.get('width_px'):
                try:
                    item['position'] = {**region_position(xml, image['width_px'], image['height_px']),
                                        'image_id': iid, 'image_node_id': image_node['Id'],
                                        'area_of_interest_id': aid, 'area_node_id': area_node['Id']}
                    if not item['position'].get('valid'):
                        warn('position', node['Id'], item['position'].get('reason', 'Undecoded region'))
                    item['acquisition_context'] = acquisition_conditions(xml)
                    item['role'] = item['position'].get('kind', 'unpositioned')
                except UnsupportedLayout as exc:
                    warn('position', node['Id'], exc)
            else:
                warn('position', node['Id'], 'Missing AOI XML or reference image dimensions')
        if any(('SmartMaps', str(n.get('AssociatedId', '')).lower()) in rows_by_id for n in trail):
            item['role'] = 'map_sum'
        if 'preview' in item['label'].casefold():
            item['role'] = 'preview'
        qnodes = [n for n in children.get(str(node['Id']).lower(), [])
                  if ('QuantResults', str(n.get('AssociatedId', '')).lower()) in rows_by_id]
        if len(qnodes) > 1:
            raise ValueError('Ambiguous spectrum quantification relationship')
        if qnodes:
            qid = str(qnodes[0]['AssociatedId']).lower()
            item['quant_result_id'] = qid
            fast['composition'].append({'quant_result_id': qid, 'spectrum_id': ident, 'node_id': node['Id'],
                                        **context(node), 'values': quant_by_id.get(qid, []),
                                        'source': 'stored_XPPResult_v2'})
        fast['spectra'].append(item)

    for index, row in enumerate(tables.get('SmartMaps', [])):
        ident = str(row['SmartMapId']).lower()
        cube = {'smartmap_id': ident, 'label': row.get('Label', ident), **object_context(ident), 'tiles': []}
        for tile_index, tile in enumerate(tables.get('TileGroups', [])):
            if str(tile['SmartMapId']).lower() != ident:
                continue
            tid = str(tile['TileGroupId']).lower()
            ref = dat_ref(tid)
            try:
                xml = exported.xml.get(('TileGroups', tile_index, 'ObjectBlob'), '')
                if not xml:
                    raise UnsupportedLayout('No decoded tile XML')
                descriptor = tile_layout(matching_blob(xml, tid), tid, tile['InstanceVersion'])
                ref.update(descriptor)
                validate_tile_envelope(source.read(ref['payload_path'], 0, min(33, source.size(ref['payload_path']))),
                                       source.size(ref['payload_path']))
                ref['decode_status'] = 'original_compressed_bytes_preserved_envelope_checked'
            except UnsupportedLayout as exc:
                warn('cube_tile', tid, exc)
            cube['tiles'].append({'tile_id': tid, **ref})
        try:
            b = matching_blob(xml_by_id['SmartMaps', ident], ident)
            cube['width_px'], cube['height_px'] = struct.unpack_from('<II', b, len(b) - 8)
            channels = {t['channel_count'] for t in cube['tiles'] if 'channel_count' in t}
            if len(channels) == 1: cube['channel_count'] = channels.pop()
        except (UnsupportedLayout, struct.error) as exc:
            warn('cube', ident, exc)
        for suffix in ('live', 'real'):
            if source.exists(f'data/{ident}{suffix}.dat'):
                cube[f'{suffix}_time_payload'] = dat_ref(ident, suffix)
        fast['smartmaps'].append(cube)

    data_dir = source_root / 'data'
    seen_paths = set()
    for path in sorted(data_dir.iterdir()):
        if not path.is_file() or path.suffix.casefold() != '.dat':
            continue
        relative = f'data/{path.name}'
        if relative.casefold() in seen_paths:
            raise ValueError('External payload path collision')
        seen_paths.add(relative.casefold())
        # Resolving through the source boundary rejects symlinks escaping root.
        with source.open(relative):
            pass
        size = source.size(relative)
        recorded = snapshot.entries.get(relative.casefold())
        if recorded is None or size != recorded['length']:
            raise ValueError('Source data inventory changed during conversion')
        external.append({'payload_id': f'payload:external:{path.stem.lower()}', 'storage': 'external',
                         'path': relative, 'offset': 0, 'length': size, 'sha256': recorded['sha256'],
                         'role': 'source_binary_payload' if relative.casefold() in referenced else 'unreferenced_source_payload'})
    if referenced - seen_paths:
        raise FileNotFoundError('Referenced payload is missing from directory registry')
    manifest = {'format': 'nesf', 'format_name': 'Native EDXSEM Source Format', 'format_version': 1,
                'created_utc': datetime.now(timezone.utc).isoformat(), 'read_policy': 'source_project_not_modified',
                'source': {'original_name': exported.source_name, 'project_kind': 'oip_sqlce'},
                'generator': {'name': 'SEM Data Converter', 'version': '0.1.0'},
                'labels': labels, 'data_nodes': _snapshot(raw_nodes),
                'source_tables': {'schema': 'nesf_source_tables_v1', 'binary_values': 'base64',
                                  'tables': _snapshot(tables), 'schema_columns': exported.schema},
                'decoded_source_xml': [{'table': t, 'row_index': i, 'column': c, 'xml': xml}
                                       for (t, i, c), xml in exported.xml.items()],
                'fast_index': fast, 'diagnostics': diagnostics,
                'normalization_status': 'partial' if diagnostics else 'decoded_pending_visual_validation'}
    snapshot.verify()
    return ProjectDataset(manifest, internal, external, internal_meta, diagnostics, snapshot)
