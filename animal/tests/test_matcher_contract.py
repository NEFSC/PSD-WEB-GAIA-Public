# ==============================================================================
# test_matcher_contract.py — Matcher Encapsulation Contract Tests
# ==============================================================================
#
# These tests ensure that db_repair.py never contains matcher-specific logic.
# The architectural requirement is that all parsing, normalization, and comparison
# logic lives behind the matcher interface in db_matchers.py.
#
# This prevents:
#   - Silent drift when adding matcher v2 or new matcher families
#   - Special-casing like "if matcher_name == 'vendor_id_ignore_typecode_v1'"
#   - Vendor-id slicing logic leaking outside the matcher module
#
# Run from project root (c:\gis\PSD-WEB-GAIA):
#   python animal\tests\test_matcher_contract.py
#   python -m pytest animal\tests\test_matcher_contract.py -v
#
# ==============================================================================

import os
import sys
import re
from pathlib import Path

# Setup Django environment when running standalone
# This must happen before importing any Django/project modules
def setup_django():
    """Configure Django settings for standalone test execution."""
    # Find project root (look for manage.py)
    current = Path(__file__).resolve()
    for parent in [current.parent, current.parent.parent, current.parent.parent.parent]:
        if (parent / 'manage.py').exists():
            project_root = parent
            break
    else:
        # Fallback: assume we're in animal/tests, project root is ../..
        project_root = Path(__file__).resolve().parent.parent.parent
    
    # Add project root to path
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    # Set Django settings module
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')
    
    try:
        import django
        django.setup()
    except Exception as e:
        print(f"Warning: Could not setup Django: {e}")
        print("Some tests may fail if they require Django imports.")

# Setup Django before other imports
setup_django()


def get_db_repair_content():
    """Load db_repair.py content for analysis."""
    # Try multiple possible locations
    possible_paths = [
        # From project root (c:\gis\PSD-WEB-GAIA)
        Path('animal') / 'management' / 'commands' / 'db_repair.py',
        # From tests directory (animal\tests)
        Path(__file__).parent.parent / 'management' / 'commands' / 'db_repair.py',
        # Fallback: same directory
        Path(__file__).parent / 'db_repair.py',
        Path('db_repair.py'),
    ]
    
    for path in possible_paths:
        if path.exists():
            return path.read_text()
    
    raise FileNotFoundError(
        f"Could not find db_repair.py. Tried: {[str(p) for p in possible_paths]}"
    )


class TestMatcherEncapsulation:
    """Tests that db_repair.py does not contain matcher-specific logic."""

    def test_no_matcher_name_conditionals(self):
        """
        db_repair.py should not branch on specific matcher names.
        
        Forbidden patterns:
          - if matcher_name == 'vendor_id_ignore_typecode_v1'
          - if matcher_name == "exact"
          - matcher_name in ['exact', 'prefix']
        """
        content = get_db_repair_content()
        
        # Pattern: if matcher_name == 'something' or if matcher_name == "something"
        conditional_pattern = r'if\s+matcher_name\s*==\s*[\'"][^\'"]+[\'"]'
        matches = re.findall(conditional_pattern, content)
        
        assert not matches, (
            f"Found matcher-specific conditionals in db_repair.py:\n"
            + "\n".join(f"  {m}" for m in matches) +
            "\n\nAll matcher logic must be in db_matchers.py"
        )

    def test_no_vendor_id_string_references(self):
        """
        db_repair.py should not reference vendor_id matcher by name 
        (except in examples/comments/help text).
        """
        content = get_db_repair_content()
        
        # Remove comments and help text strings for this check
        # We only care about actual code references
        lines = content.split('\n')
        code_lines = []
        in_docstring = False
        
        for line in lines:
            stripped = line.strip()
            
            # Track docstring state
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            
            # Skip comment lines
            if stripped.startswith('#'):
                continue
            
            # Skip lines that are purely string literals (help text)
            if stripped.startswith('"') or stripped.startswith("'"):
                continue
                
            code_lines.append(line)
        
        code_content = '\n'.join(code_lines)
        
        # Should not have vendor_id_ignore_typecode hardcoded in logic
        # (it's fine in help examples and comments, but not in actual conditionals)
        forbidden_patterns = [
            r"if.*vendor_id_ignore_typecode",
            r"elif.*vendor_id_ignore_typecode",
            r"vendor_id_ignore_typecode.*==",
            r"==.*vendor_id_ignore_typecode",
        ]
        
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, code_content)
            assert not matches, (
                f"Found vendor_id matcher hardcoded in db_repair.py logic:\n"
                f"Pattern: {pattern}\n"
                f"Matches: {matches}\n"
                "\nAll matcher logic must be in db_matchers.py"
            )

    def test_no_vendor_id_slicing_patterns(self):
        """
        db_repair.py should not contain vendor_id position slicing.
        
        The vendor_id format uses positions:
          - 0-12: timestamp
          - 13-18: type code
          - 19+: catalog ID
        
        These slice patterns should only appear in db_matchers.py.
        """
        content = get_db_repair_content()
        
        # Remove docstrings and comments
        lines = content.split('\n')
        code_lines = [
            line for line in lines 
            if not line.strip().startswith('#') 
            and '"""' not in line
            and "'''" not in line
        ]
        code_content = '\n'.join(code_lines)
        
        # Vendor-id specific slice patterns
        forbidden_slices = [
            r'\[:13\]',      # timestamp slice
            r'\[13:18\]',    # type code slice
            r'\[14:18\]',    # type code variant
            r'\[19:\]',      # catalog slice
            r'\[:12\]',      # timestamp variant
        ]
        
        for pattern in forbidden_slices:
            matches = re.findall(pattern, code_content)
            assert not matches, (
                f"Found vendor_id slicing pattern in db_repair.py:\n"
                f"Pattern: {pattern}\n"
                f"This logic must be in db_matchers.py"
            )


class TestMatcherInterface:
    """Tests that all matchers implement the required interface."""

    def test_all_matchers_have_required_keys(self):
        """Every registered matcher must have match, normalize, supports_dict_lookup."""
        # Import from the actual module
        try:
            from db_matchers import MATCHERS
        except ImportError:
            from animal.utils.db_matchers import MATCHERS
        
        required_keys = ['match', 'normalize', 'supports_dict_lookup', 'description']
        
        for name, config in MATCHERS.items():
            for key in required_keys:
                assert key in config, (
                    f"Matcher '{name}' missing required key '{key}'\n"
                    f"All matchers must implement: {required_keys}"
                )

    def test_all_matchers_have_callable_functions(self):
        """match and normalize must be callable."""
        try:
            from db_matchers import MATCHERS
        except ImportError:
            from animal.utils.db_matchers import MATCHERS
        
        for name, config in MATCHERS.items():
            assert callable(config['match']), (
                f"Matcher '{name}' match is not callable"
            )
            assert callable(config['normalize']), (
                f"Matcher '{name}' normalize is not callable"
            )

    def test_supports_dict_lookup_is_boolean(self):
        """supports_dict_lookup must be a boolean."""
        try:
            from db_matchers import MATCHERS, supports_dict_lookup
        except ImportError:
            from animal.utils.db_matchers import MATCHERS, supports_dict_lookup
        
        for name in MATCHERS:
            result = supports_dict_lookup(name)
            assert isinstance(result, bool), (
                f"Matcher '{name}' supports_dict_lookup returned {type(result)}, expected bool"
            )

    def test_normalizer_returns_none_for_none_input(self):
        """All normalizers should handle None input gracefully."""
        try:
            from db_matchers import MATCHERS, get_normalizer
        except ImportError:
            from animal.utils.db_matchers import MATCHERS, get_normalizer
        
        for name in MATCHERS:
            normalizer = get_normalizer(name)
            result = normalizer(None)
            assert result is None, (
                f"Matcher '{name}' normalizer should return None for None input, "
                f"got {result}"
            )

    def test_matcher_returns_false_for_none_input(self):
        """All matchers should return False when either input is None."""
        try:
            from db_matchers import MATCHERS, get_matcher
        except ImportError:
            from animal.utils.db_matchers import MATCHERS, get_matcher
        
        for name in MATCHERS:
            matcher = get_matcher(name)
            
            assert matcher(None, "value") == False, (
                f"Matcher '{name}' should return False when left is None"
            )
            assert matcher("value", None) == False, (
                f"Matcher '{name}' should return False when right is None"
            )
            assert matcher(None, None) == False, (
                f"Matcher '{name}' should return False when both are None"
            )


class TestMatcherConsistency:
    """Tests that match and normalize are consistent."""

    def test_normalizer_produces_matchable_keys(self):
        """
        For matchers that support dict lookup, normalizing both sides of a 
        matching pair should produce equal normalized keys.
        """
        try:
            from db_matchers import MATCHERS, get_matcher, get_normalizer, supports_dict_lookup
        except ImportError:
            from animal.utils.db_matchers import MATCHERS, get_matcher, get_normalizer, supports_dict_lookup
        
        # Test cases for each matcher type
        test_cases = {
            'exact': [
                ('hello', 'hello', True),
                ('hello', 'world', False),
            ],
            'vendor_id_ignore_typecode_v1': [
                # Same timestamp + catalog, different type code
                ('21MAR21152115-S1BS-507583593010_01_P003', 
                 '21MAR21152115-M1BS-507583593010_01_P003', True),
                # Different timestamp
                ('21MAR21152115-S1BS-507583593010_01_P003',
                 '24JUL04205750-M1BS-508530682010_02_P006', False),
            ],
        }
        
        for matcher_name, cases in test_cases.items():
            if matcher_name not in MATCHERS:
                continue
                
            matcher = get_matcher(matcher_name)
            normalizer = get_normalizer(matcher_name)
            use_dict = supports_dict_lookup(matcher_name)
            
            for left, right, should_match in cases:
                # Verify matcher gives expected result
                actual_match = matcher(left, right)
                assert actual_match == should_match, (
                    f"Matcher '{matcher_name}' mismatch:\n"
                    f"  left={left}\n"
                    f"  right={right}\n"
                    f"  expected={should_match}, got={actual_match}"
                )
                
                # If dict lookup is supported, normalized keys should be consistent
                if use_dict:
                    left_norm = normalizer(left)
                    right_norm = normalizer(right)
                    
                    if should_match:
                        assert left_norm == right_norm, (
                            f"Matcher '{matcher_name}' normalizer inconsistency:\n"
                            f"  Matcher says {left} and {right} match,\n"
                            f"  but normalized keys differ: {left_norm} != {right_norm}"
                        )
                    else:
                        assert left_norm != right_norm, (
                            f"Matcher '{matcher_name}' normalizer inconsistency:\n"
                            f"  Matcher says {left} and {right} don't match,\n"
                            f"  but normalized keys are equal: {left_norm}"
                        )


# ==============================================================================
# Standalone execution
# ==============================================================================

if __name__ == '__main__':
    import sys
    
    print("=" * 70)
    print("MATCHER CONTRACT TESTS")
    print("=" * 70)
    print()
    
    # Run tests manually if pytest not available
    test_classes = [
        TestMatcherEncapsulation,
        TestMatcherInterface,
        TestMatcherConsistency,
    ]
    
    passed = 0
    failed = 0
    
    for test_class in test_classes:
        print(f"\n{test_class.__name__}:")
        print("-" * 50)
        
        instance = test_class()
        for method_name in dir(instance):
            if method_name.startswith('test_'):
                try:
                    getattr(instance, method_name)()
                    print(f"  ✓ {method_name}")
                    passed += 1
                except AssertionError as e:
                    print(f"  ✗ {method_name}")
                    print(f"    {str(e)[:200]}")
                    failed += 1
                except Exception as e:
                    print(f"  ✗ {method_name} (error: {e})")
                    failed += 1
    
    print()
    print("=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)
    
    sys.exit(0 if failed == 0 else 1)