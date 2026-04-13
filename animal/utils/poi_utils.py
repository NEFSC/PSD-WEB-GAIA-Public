"""
POI loading utilities for GAIA.

Filename parsing for GeoJSON files produced by
generate_interesting_points. Pure business logic — no
command infrastructure.
"""
# ----------------------------------------------------------------------
# ----- poi_utils.py --------------------------------------------------
# ----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  POI loading utilities. Filename parsing for GeoJSON
#              files produced by generate_interesting_points.
#
#    tickets:  GAIFAGP-451 (load GeoJSON POIs)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - GeoJSON filenames follow the convention:
#        {vendor_id}_{processing_suffix}_{params}.geojson
#      - Processing suffix encodes EPSG code as trailing
#        digits (e.g., u08mr32619 -> EPSG:32619)
#      - vendor_id is the file-level imagery identifier
#        (not catalog_id — see DL-017)
#      - Pattern mirrors inventory.py
#        _parse_vendor_id_from_blob
#
# ----------------------------------------------------------------------

import re


# Matches the processing suffix injected by the imagery pipeline.
# Example: _u08mr32619 in filename
#   21APR24154044-S1BS-506967344060_01_P005_u08mr32619_cog_...
# Group 1 captures the EPSG code (trailing digits).
_PROCESSING_SUFFIX_RE = re.compile(r'_u\d{2}[a-z]{2}(\d+)')

# Fallback match for vendor identifiers when the processing suffix is absent
# or inconsistent. Example token:
#   21SEP28215129-505662347010_01_P020
_VENDOR_ID_FALLBACK_RE = re.compile(
    r'(\d{2}[A-Za-z]{3}\d{6,}-[A-Za-z0-9]+_\d{2}_P\d{3})',
    re.IGNORECASE,
)


def parse_geojson_filename(filename: str) -> dict:
    """
    Extract vendor_id and epsg_code from a GeoJSON filename.

    Filename convention from generate_interesting_points output:
      {vendor_id}_{processing_suffix}_{params}.geojson
    Processing suffix contains EPSG code as trailing digits.

    Example:
      21APR24154044-S1BS-506967344060_01_P005_u08mr32619_cog_area-2_difference-auto99_95.geojson
      -> vendor_id = 21APR24154044-S1BS-506967344060_01_P005
      -> epsg_code = 32619

    Args:
        filename: GeoJSON filename (not full path).

    Returns:
        dict with 'vendor_id' and 'epsg_code' keys.

    Raises:
        ValueError: If filename doesn't match expected convention.
    """
    stem = filename
    if stem.lower().endswith('.geojson'):
        stem = stem[:-8]

    match = _PROCESSING_SUFFIX_RE.search(stem)
    if not match:
        raise ValueError(
            f"Cannot parse processing suffix from '{filename}'. "
            f"Expected pattern: {{vendor_id}}_u##XX{{epsg}}..."
        )

    vendor_id = stem[:match.start()]
    epsg_code = match.group(1)

    if not vendor_id:
        raise ValueError(
            f"Empty vendor_id parsed from '{filename}'."
        )

    return {
        'vendor_id': vendor_id,
        'epsg_code': epsg_code,
    }


def parse_vendor_id_from_geojson_filename(filename: str) -> str:
    """Extract a vendor_id from a GeoJSON filename.

    Uses ``parse_geojson_filename`` first (strict/primary path), then falls
    back to a token-based vendor-id regex for slight naming differences.
    """
    base_name = (filename or '').split('/')[-1].split('\\')[-1]
    stem = base_name[:-8] if base_name.lower().endswith('.geojson') else base_name

    if not stem:
        raise ValueError('Filename is empty.')

    try:
        parsed = parse_geojson_filename(base_name)
        return parsed['vendor_id']
    except ValueError:
        match = _VENDOR_ID_FALLBACK_RE.search(stem)
        if match:
            return match.group(1)

    raise ValueError(
        f"Cannot parse vendor_id from '{base_name}'. "
        "Expected a vendor token like 21SEP28215129-505662347010_01_P020."
    )


def parse_vendor_id_from_geojson_name(name_value: str) -> str:
    """Extract a vendor_id from a GeoJSON ``name`` field value."""
    candidate = (name_value or '').strip()
    if not candidate:
        raise ValueError(
            "GeoJSON must include a non-empty top-level 'name' field."
        )

    try:
        parsed = parse_geojson_filename(candidate)
        return parsed['vendor_id']
    except ValueError:
        match = _VENDOR_ID_FALLBACK_RE.search(candidate)
        if match:
            return match.group(1)

    raise ValueError(
        f"Cannot parse vendor_id from GeoJSON name '{candidate}'. "
        "Expected a value like 23NOV15152207-S1BS-507980222010_02_P001_u08mr32619."
    )


def parse_vendor_id_from_geojson_payload(payload: dict) -> str:
    """Extract a vendor_id from a GeoJSON payload's top-level ``name`` field."""
    if not isinstance(payload, dict):
        raise ValueError('Uploaded GeoJSON must decode to a JSON object.')

    return parse_vendor_id_from_geojson_name(payload.get('name'))


def normalize_vendor_match_key(vendor_id: str) -> str:
    """Normalize vendor IDs for equivalence matching.

    Matching is case-insensitive and treats P1BS/S1BS variants as equivalent.
    """
    normalized = str(vendor_id or '').strip().upper()
    return normalized.replace('-P1BS-', '-S1BS-')
