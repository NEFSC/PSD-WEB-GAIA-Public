# ------------------------------------------------------------------------------
# ----- db_matchers.py ---------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Named matchers for soft-link joins in db_repair operations.
#              Each matcher is a callable that returns True if two values match.
#              Each matcher also has a normalizer for O(1) dictionary lookups.
#
#    tickets:  GAIFAGP-467, GAIFAGP-468, GAIFAGP-470
#
#    DESIGN PRINCIPLES:
#      - Matchers are registered by name, referenced in soft_links.yaml
#      - Complex matching logic lives here with unit tests, not in YAML
#      - All matchers must handle None/empty gracefully
#      - Normalizers enable O(N+M) algorithms instead of O(N×M) nested loops
#
# ------------------------------------------------------------------------------

from typing import Callable, Optional, Dict, Any, Tuple, Union


# -----------------------------------------------------------------------------
# Matcher Functions
# -----------------------------------------------------------------------------

def exact_matcher(a: Optional[str], b: Optional[str]) -> bool:
    """Exact string equality match."""
    if a is None or b is None:
        return False
    return str(a) == str(b)


def exact_normalizer(key: Optional[str]) -> Optional[str]:
    """Normalize key for exact matching — returns key as-is."""
    if key is None:
        return None
    return str(key)


def prefix_matcher(a: Optional[str], b: Optional[str]) -> bool:
    """Either value is a prefix of the other."""
    if a is None or b is None:
        return False
    a_str, b_str = str(a), str(b)
    return a_str.startswith(b_str) or b_str.startswith(a_str)


def prefix_normalizer(key: Optional[str]) -> Optional[str]:
    """
    Normalize key for prefix matching.
    
    NOTE: Prefix matching is inherently O(N×M) for arbitrary strings.
    This normalizer returns the key as-is, meaning prefix matching
    will fall back to nested loop comparison. Use exact or 
    vendor_id_ignore_typecode_v1 for large datasets.
    """
    if key is None:
        return None
    return str(key)


def vendor_id_ignore_typecode_v1(a: Optional[str], b: Optional[str]) -> bool:
    """
    Match vendor_id values ignoring type code (positions 14-19).
    
    vendor_id format: DDMMMYYHHMMSS-XXXX-CATALOGID_XX_PNNN
      - Positions 0-12 (13 chars): timestamp (e.g., 24JUL04205750)
      - Positions 13-18 (6 chars): type code with dashes (e.g., -M1BS-)
      - Positions 19+: catalog/scene ID (e.g., 508530682010_02_P006)
    
    POI may have S1BS, EE may have M1BS or P1BS for same scene.
    Match on timestamp + catalog, ignore type code.
    
    Examples:
        21MAR21152115-S1BS-507583593010_01_P003  (POI)
        21MAR21152115-M1BS-507583593010_01_P003  (EE)
        -> MATCH (same timestamp, same catalog)
        
        21MAR21152115-S1BS-507583593010_01_P003  (POI)
        24JUL04205750-M1BS-508530682010_02_P006  (EE)
        -> NO MATCH (different timestamp)
    """
    if a is None or b is None:
        return False
    
    a_str, b_str = str(a), str(b)
    
    if len(a_str) < 20 or len(b_str) < 20:
        return False
    
    # Compare timestamp (first 13 chars) and catalog (position 19 onward)
    return (a_str[:13] == b_str[:13]) and (a_str[19:] == b_str[19:])


def vendor_id_ignore_typecode_v1_normalizer(key: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Normalize vendor_id by extracting timestamp + catalog, ignoring type code.
    
    Returns tuple of (timestamp, catalog) for dictionary lookup.
    Returns None if key is malformed (too short).
    """
    if key is None:
        return None
    
    key_str = str(key)
    if len(key_str) < 20:
        return None
    
    # Extract timestamp (first 13 chars) and catalog (position 19 onward)
    return (key_str[:13], key_str[19:])


# -----------------------------------------------------------------------------
# Matcher Registry
# -----------------------------------------------------------------------------

MATCHERS: Dict[str, Dict[str, Any]] = {
    'exact': {
        'match': exact_matcher,
        'normalize': exact_normalizer,
        'description': 'Exact string equality match.',
        'supports_dict_lookup': True,
    },
    'prefix': {
        'match': prefix_matcher,
        'normalize': prefix_normalizer,
        'description': 'Either value is a prefix of the other.',
        'supports_dict_lookup': False,  # Inherently O(N×M)
    },
    'vendor_id_ignore_typecode_v1': {
        'match': vendor_id_ignore_typecode_v1,
        'normalize': vendor_id_ignore_typecode_v1_normalizer,
        'description': 'Match vendor_id values ignoring type code (positions 14-19).',
        'supports_dict_lookup': True,
    },
}


def get_matcher(name: str) -> Callable[[Optional[str], Optional[str]], bool]:
    """
    Get a matcher function by name.
    
    Args:
        name: Matcher name (must be in MATCHERS registry)
        
    Returns:
        Matcher callable
        
    Raises:
        ValueError: If matcher name not found
    """
    if name not in MATCHERS:
        available = ', '.join(sorted(MATCHERS.keys()))
        raise ValueError(f"Unknown matcher '{name}'. Available: {available}")
    return MATCHERS[name]['match']


def get_normalizer(name: str) -> Callable[[Optional[str]], Any]:
    """
    Get a normalizer function by name.
    
    Normalizers convert keys to a canonical form for dictionary lookups,
    enabling O(N+M) algorithms instead of O(N×M) nested loops.
    
    Args:
        name: Matcher name (must be in MATCHERS registry)
        
    Returns:
        Normalizer callable
        
    Raises:
        ValueError: If matcher name not found
    """
    if name not in MATCHERS:
        available = ', '.join(sorted(MATCHERS.keys()))
        raise ValueError(f"Unknown matcher '{name}'. Available: {available}")
    return MATCHERS[name]['normalize']


def supports_dict_lookup(name: str) -> bool:
    """
    Check if a matcher supports efficient dictionary lookups.
    
    Matchers that return True can use O(N+M) algorithms.
    Matchers that return False require O(N×M) nested loops.
    
    Args:
        name: Matcher name
        
    Returns:
        True if dictionary lookup is supported
    """
    if name not in MATCHERS:
        return False
    return MATCHERS[name].get('supports_dict_lookup', False)


def list_matchers() -> Dict[str, str]:
    """Return dict of matcher names to their descriptions."""
    return {name: info['description'] for name, info in MATCHERS.items()}