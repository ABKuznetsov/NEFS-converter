"""Import-safe adaptations of MIT aztec-archive-recovery decoders.

Spectrum, image and tile layouts and named_doubles originate in the pinned
upstream snapshot. Generalized labels/counts, explicit checks and XML input.
quant_values is an additional decoder tested against stored project results;
it is not supplied by upstream and does not invoke vendor libraries.
"""

import base64
import math
import struct
import uuid
import xml.etree.ElementTree as ET

XSI_TYPE = '{http://www.w3.org/2001/XMLSchema-instance}type'
SYMBOLS = ('? H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
           'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce '
           'Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At '
           'Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn '
           'Nh Fl Mc Lv Ts Og').split()


class UnsupportedLayout(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, count: int) -> bytes:
        if count < 0 or count > len(self.data) - self.pos:
            raise ValueError('Truncated binary field')
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def string(self) -> str:
        length = 0
        for i in range(5):
            byte = self.take(1)[0]
            if i == 4 and byte > 7:
                raise ValueError('Invalid .NET string length')
            length |= (byte & 127) << (7 * i)
            if not byte & 128:
                return self.take(length).decode('utf-8')
        raise ValueError('Invalid .NET string prefix')


def xml_root(xml: str):
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('DTD/entity declarations are not accepted')
    return ET.fromstring(xml)


def local_name(element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def blobs(element) -> list[bytes]:
    return [base64.b64decode(''.join((x.text or '').split()), validate=True)
            for x in element if local_name(x) == 'Blob']


def matching_blob(xml: str, ident: str) -> bytes:
    guid = uuid.UUID(ident).bytes_le
    matches = [b for b in blobs(xml_root(xml)) if len(b) >= 20 and b[4:20] == guid]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one identity-matched blob: {ident}')
    return matches[0]


def decode_spectrum(blob: bytes, expected_id: str, expected_label: str, *, expected_instance: int = 1) -> dict:
    reader = Reader(blob)
    if reader.unpack('<I')[0] != 2:
        raise UnsupportedLayout('Unsupported stored spectrum version')
    if str(uuid.UUID(bytes_le=reader.take(16))).casefold() != expected_id.casefold():
        raise ValueError('Spectrum identity mismatch')
    instance, flag = reader.unpack('<IB')
    if instance != expected_instance or instance < 1 or flag != 0:
        raise UnsupportedLayout('Unsupported spectrum instance/flag')
    if reader.string() != expected_label:
        raise ValueError('Spectrum label mismatch')
    count = reader.unpack('<I')[0]
    if not 0 < count <= 65536:
        raise UnsupportedLayout('Unsupported spectrum channel count')
    count_bytes = reader.take(count * 4)
    gain, offset, *flags = reader.unpack('<dd4B')
    if reader.pos != len(blob) or flags != [1, 1, 1, 1]:
        raise UnsupportedLayout('Unsupported spectrum trailing fields/flags')
    if not math.isfinite(gain) or gain <= 0 or not math.isfinite(offset):
        raise ValueError('Invalid stored energy calibration')
    counts = struct.unpack('<' + str(count) + 'I', count_bytes)
    if struct.pack('<' + str(count) + 'I', *counts) != count_bytes:
        raise ValueError('Spectrum count roundtrip differs')
    return {'count_bytes': count_bytes, 'channel_count': count, 'gain_eV': gain,
            'offset_eV': offset, 'total_counts': sum(counts), 'max_count': max(counts)}


def image_layout(xml: str) -> dict:
    root = xml_root(xml)
    core = next((x for x in root.iter() if local_name(x) == 'imageDataCore'), None)
    if core is None:
        raise UnsupportedLayout('Image has no imageDataCore')
    kind = core.attrib.get(XSI_TYPE, '').split(':')[-1]
    types = {'FoldedImageShort': ('int16_le', 2), 'FoldedImageFloat': ('float32_le', 4)}
    if kind not in types:
        raise UnsupportedLayout(f'Unsupported image core: {kind}')
    dtype, itemsize = types[kind]
    levels, expected = [], 0
    for element in core.iter():
        if local_name(element) != 'Blob':
            continue
        raw = base64.b64decode(element.text or '', validate=True)
        if len(raw) != 29:
            raise ValueError('Invalid TileSizeInfo length')
        version, width, height, offset, flag, stored_size, tail = struct.unpack('<IIIQBII', raw)
        if version != 1 or flag != 1 or tail != 0 or stored_size != itemsize:
            raise UnsupportedLayout('Unsupported image pyramid descriptor')
        if not 0 < width <= 100000 or not 0 < height <= 100000 or offset != expected:
            raise ValueError('Invalid image dimensions or scalar offset')
        levels.append({'width': width, 'height': height, 'scalar_offset': offset,
                       'byte_offset': offset * itemsize, 'byte_length': width * height * itemsize})
        expected += width * height
    if not levels:
        raise ValueError('Empty image pyramid')
    return {'dtype': dtype, 'itemsize': itemsize, 'levels': levels, 'total_bytes': expected * itemsize,
            'short_signedness': 'upstream_int16_interpretation_not_universally_established' if itemsize == 2 else None}


def tile_layout(blob: bytes, expected_id: str, instance: int) -> dict:
    if len(blob) != 60 or struct.unpack_from('<I', blob)[0] != 2:
        raise UnsupportedLayout('Unsupported tile descriptor')
    guid = uuid.UUID(expected_id).bytes_le
    if blob[4:20] != guid or blob[44:60] != guid:
        raise ValueError('Tile identity mismatch')
    version, height, width, y, x, channels = struct.unpack_from('<6I', blob, 20)
    if version != instance or not 0 < width * height <= 16_777_216 or not 0 < channels <= 65536:
        raise UnsupportedLayout('Unsupported tile dimensions/instance/channels')
    return {'width': width, 'height': height, 'x': x, 'y': y, 'channel_count': channels}


def validate_tile_envelope(header: bytes, original_size: int) -> None:
    if len(header) < 33:
        raise ValueError('Truncated compressed tile envelope')
    if header[:25] != b'OINA.Ed.MapTileGroup\0\x03LZO':
        raise UnsupportedLayout('Unknown tile payload codec')
    uncompressed, compressed = struct.unpack_from('<II', header, 25)
    if uncompressed <= 0 or compressed <= 0 or compressed != original_size - 33:
        raise ValueError('Compressed tile length does not match envelope')


def named_doubles(data: bytes) -> dict:
    reader = Reader(data)
    version, count = reader.unpack('<II')
    if version != 1 or count > 10000:
        raise UnsupportedLayout('Unsupported named conditions')
    keys = []
    for _ in range(count):
        if reader.take(1) != b'\0':
            raise UnsupportedLayout('Unsupported condition name flag')
        keys.append(reader.string())
    if len(set(keys)) != len(keys) or reader.unpack('<I')[0] != count:
        raise ValueError('Invalid named conditions cardinality')
    values = reader.unpack('<' + 'd' * count)
    if reader.pos != len(data) or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid named conditions values')
    return dict(zip(keys, values))


def quant_values(xml: str) -> list[dict]:
    result = []
    for element in xml_root(xml).iter():
        if element.attrib.get(XSI_TYPE, '').split(':')[-1] != 'XPPResult':
            continue
        values = blobs(element)
        if len(values) != 1:
            raise ValueError('Invalid XPP result blob count')
        reader = Reader(values[0])
        if reader.unpack('<I')[0] != 2:
            raise UnsupportedLayout('Unsupported XPP result version')
        reader.take(16)
        z = reader.unpack('<I')[0]
        atomic = reader.unpack('<d')[0]
        reader.take(41)
        weight, sigma, ratio = reader.unpack('<3d')
        if reader.take(1) != b'\0':
            raise UnsupportedLayout('Unsupported XPP standard flag')
        standard = reader.string()
        if reader.take(1)[0] not in (0, 1) or reader.take(1)[0] not in (0, 1):
            raise UnsupportedLayout('Unsupported XPP suffix')
        line, flag = reader.unpack('<IB')
        if reader.pos != len(reader.data) or flag not in (0, 1):
            raise UnsupportedLayout('Unsupported XPP trailing fields')
        if not 0 < z < len(SYMBOLS) or not all(math.isfinite(x) for x in (atomic, weight, sigma, ratio)):
            raise ValueError('Invalid XPP values')
        if not 0 <= atomic <= 100.01 or not 0 <= weight <= 1000 or sigma < 0 or ratio < 0:
            raise ValueError('XPP values outside accepted range')
        result.append({'element': SYMBOLS[z], 'atomic_number': z, 'weight_percent': weight,
                       'weight_sigma': sigma, 'atomic_percent': atomic, 'k_ratio': ratio,
                       'standard': standard, 'line_code': line})
    return result
