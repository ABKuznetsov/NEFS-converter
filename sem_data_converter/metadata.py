"""Source AOI geometry and acquisition declarations, without guessed pixels."""

import math
import struct

from .decoders import UnsupportedLayout, XSI_TYPE, blobs, local_name, named_doubles, xml_root


def acquisition_conditions(xml: str) -> dict:
    out = {}
    for element in xml_root(xml).iter():
        if local_name(element) in ('columnConditions', 'stageConditions'):
            values = blobs(element)
            if values:
                out[local_name(element)] = named_doubles(values[0])
    return out


def image_scale(xml: str, width: int, height: int) -> dict:
    conditions = acquisition_conditions(xml)
    mag = conditions.get('columnConditions', {}).get('Magnification')
    scan = next((e for e in xml_root(xml).iter() if local_name(e) == 'ScanCalibration'), None)
    if scan is None or not mag or mag <= 0:
        return {}
    raw = blobs(scan)
    if not raw or len(raw[0]) != 20:
        raise UnsupportedLayout('Unsupported scan calibration')
    version, aspect, field = struct.unpack('<Idd', raw[0])
    if version != 1 or not all(math.isfinite(x) and x > 0 for x in (aspect, field)):
        raise UnsupportedLayout('Unsupported scan calibration values')
    pixel = field / mag * 1000 / width
    return {'um_per_pixel_x': pixel, 'um_per_pixel_y': pixel,
            'field_width_um': pixel * width, 'field_height_um': pixel * height,
            'source': 'stored_scan_calibration_and_named_magnification',
            'confidence': 'unit_interpretation_requires_external_validation',
            'stored_reference_field': field, 'stored_aspect': aspect, 'magnification': mag,
            'unit_assumption': 'reference_field_is_mm_at_magnification_1x'}


def region_position(xml: str, width: int, height: int) -> dict:
    region = next((e for e in xml_root(xml) if local_name(e) == 'region'), None)
    if region is None:
        return {'valid': False, 'reason': 'no_region'}
    kind = region.attrib.get(XSI_TYPE, '').split(':')[-1]
    raw = blobs(region)
    if kind in ('RectangleRegion', 'FullFieldRegion'):
        candidates = [b for b in raw if len(b) == 36]
        if len(candidates) != 1:
            raise UnsupportedLayout('Unsupported rectangle/full-field region')
        version, x, y, w, h = struct.unpack('<I4d', candidates[0])
        if version != 1 or not all(math.isfinite(v) for v in (x, y, w, h)) or w < 0 or h < 0:
            raise ValueError('Invalid region geometry')
        x, y, w, h = x * width, y * width, w * width, h * width
        name = 'rectangle' if kind == 'RectangleRegion' else 'full_field'
        if name == 'full_field':
            x, y = x + w / 2, y + h / 2
    elif kind == 'PointRegion':
        candidates = [b for b in raw if len(b) == 20]
        if len(candidates) != 1:
            raise UnsupportedLayout('Unsupported point region')
        version, x, y = struct.unpack('<I2d', candidates[0])
        if version != 1 or not all(math.isfinite(v) for v in (x, y)):
            raise ValueError('Invalid point geometry')
        x, y, w, h, name = x * width, y * width, 0., 0., 'point'
    else:
        return {'valid': False, 'reason': f'unsupported_region:{kind}'}
    return {'valid': True, 'kind': name, 'coordinate_system': 'electron_image_pixels',
            'x_px': x, 'y_px': y, 'width_px': w, 'height_px': h,
            'anchor': 'top_left' if name == 'rectangle' else 'center',
            'center_x_px': x + w / 2 if name == 'rectangle' else x,
            'center_y_px': y + h / 2 if name == 'rectangle' else y,
            'image_width_px': width, 'image_height_px': height,
            'source': f'AreaOfInterests.{kind}',
            'transform': 'normalized_width_units_top_aligned_bottom_cropped',
            'within_image': 0 <= x <= width and 0 <= y <= height}
