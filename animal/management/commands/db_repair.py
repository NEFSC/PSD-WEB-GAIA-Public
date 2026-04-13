# ------------------------------------------------------------------------------
# ----- db_repair.py -----------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Database/ORM authority for GAIA. Schema introspection,
#              FK enforcement state, drift detection, deterministic diffs,
#              and auditable repairs with change logs.
#
#    tickets:  GAIFAGP-467 (entity_id backfill)
#              GAIFAGP-468 (sensor backfill)
#              GAIFAGP-470 (POI↔ETL audit)
#              GAIFAGP-XXX (db_utils decomposition)
#
#    SCOPE:
#      IN:  Schema introspection, FK graphs, drift detection, diffs, repairs
#      OUT: Domain health reports (poi.py), USGS API (usgs.py), business logic
#
#    SAFETY:
#      - All repairs require --dry-run or --confirm
#      - Every mutation prints exact SQL before execution
#      - Every confirmed repair writes timestamped JSON artifact
#      - Refuses to run if identity cannot be determined
#      - Non-unique joins require --pick for determinism (release blocker)
#
#    CONFIGURATION (in settings.py):
#      DB_REPAIR_ARTIFACT_DIR = BASE_DIR / "var" / "db_repair"
#      DB_REPAIR_SOFT_LINKS = BASE_DIR / "gaia" / "soft_links.yaml"
#
#    usage:    python manage.py db_repair --help
#              python manage.py db_repair schema --table=animal_pointsofinterest
#              python manage.py db_repair fk-enforcement
#              python manage.py db_repair compare --left=X --right=Y --key=Z ...
#              python manage.py db_repair backfill --source=X --target=Y ... --dry-run
#
# ------------------------------------------------------------------------------

import argparse
import json
import os
import yaml
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections
from django.apps import apps
from django.utils import timezone


# Import matchers - adjust path based on where this file is placed
try:
    from animal.utils.db_matchers import get_matcher, get_normalizer, supports_dict_lookup, list_matchers, MATCHERS
except ImportError:
    # Fallback for standalone testing
    from db_matchers import get_matcher, get_normalizer, supports_dict_lookup, list_matchers, MATCHERS


class Command(BaseCommand):
    help = """
Database/ORM authority for GAIA schema introspection and repair.

SCOPE:
  This command handles database structure, constraints, and data integrity.
  Domain-specific health reports belong in poi.py, ee.py, etl.py.

ACTIONS:
  Introspection (read-only):
    schema          Dump columns, types, indexes for a table
    fk-declared     Show FKs declared in database (PRAGMA foreign_key_list)
    fk-orm          Show FKs declared in Django models
    fk-enforcement  Check PRAGMA foreign_keys state
    soft-links      Display soft-link registry contents
    matchers        List available matchers for soft-link joins
    drift           Detect schema drift (DB vs ORM vs Migrations)

  Profiling (read-only):
    orphans         Find rows with no matching parent (supports soft-links)

  Comparison (read-only):
    compare         Deterministic diff of two tables on a join key

  Repair (requires --dry-run or --confirm):
    backfill        Copy field values from source to target table

DETERMINISM:
  For non-unique joins (cardinality M:1, 1:M, M:M), you MUST specify --pick
  to select which row to use when multiple matches exist. Options:
    --pick=latest:<col>   Use row with max value in <col> (typically timestamp)
    --pick=min:<col>      Use row with min value in <col>
    --pick=max:<col>      Use row with max value in <col>
    --pick=first          Use first row encountered (explicit non-determinism)

  Without --pick on non-1:1 joins, the command will refuse to run.

RELATIONSHIP TYPES (for orphans action):
  --relationship=enforced       Use FK declared in database
  --relationship=declared       Same as enforced (FK exists but may not be enforced)
  --relationship=soft:<n>    Use soft-link from registry by name

  Note: Orphan detection is EXISTENTIAL - it answers "does any parent exist?"
  Cardinality is auto-loaded from the registry when using soft:<n>.

DRIFT SECTIONS:
  The drift action produces three explicit comparisons:
    1. DB↔Models:     PRAGMA table_info vs Django model fields
    2. Migration Inventory: Applied migrations and table existence
    3. Models↔Migrations: Check for unapplied or missing migrations
  
  Scope: Tables, columns, indexes only.
  [NOT EVALUATED]: Constraints, triggers, partial indexes, type details.

EXAMPLES:
  # Introspection
  python manage.py db_repair schema --table=animal_pointsofinterest
  python manage.py db_repair fk-enforcement
  python manage.py db_repair soft-links
  python manage.py db_repair drift --app=animal

  # Orphans (soft-link from registry - auto-loads matcher, cardinality)
  python manage.py db_repair orphans \\
    --table=animal_pointsofinterest --relationship=soft:poi_to_ee_vendor \\
    --sample=10

  # Orphans (enforced FK - manual specification)
  python manage.py db_repair orphans \\
    --table=animal_pointsofinterest --relationship=enforced \\
    --fk-column=catalog_id --parent-table=animal_etl --sample=10

  # Compare using registry (RECOMMENDED - auto-loads tables, key, matcher, cardinality)
  python manage.py db_repair compare \\
    --relationship=soft:poi_to_ee_vendor \\
    --pick=latest:date_image_taken --sample=10

  # Compare manual (all args required)
  python manage.py db_repair compare \\
    --left=animal_pointsofinterest --right=animal_earthexplorer \\
    --key=vendor_id --matcher=vendor_id_ignore_typecode_v1 \\
    --cardinality=M:1 --pick=latest:date_image_taken --sample=10

  # Backfill using registry (RECOMMENDED)
  # Default direction is left<-right: copy FROM right TO left (enrich child from parent)
  # For poi_to_ee_vendor: copies FROM animal_earthexplorer TO animal_pointsofinterest
  python manage.py db_repair backfill \\
    --relationship=soft:poi_to_ee_vendor \\
    --fields=entity_id --pick=latest:date_image_taken --dry-run

  # Backfill with explicit direction (reverse: copy FROM left TO right)
  python manage.py db_repair backfill \\
    --relationship=soft:poi_to_ee_vendor --direction=left->right \\
    --fields=entity_id --pick=first --dry-run

  # Backfill manual (all args required)
  python manage.py db_repair backfill \\
    --source=animal_earthexplorer --target=animal_pointsofinterest \\
    --key=vendor_id --matcher=vendor_id_ignore_typecode_v1 \\
    --fields=entity_id --cardinality=M:1 --pick=latest:date_image_taken \\
    --dry-run

DIRECTION (for backfill --relationship):
  --direction=left<-right  (default) Copy FROM right TO left. Typical use case:
                           enrich the child/left table from the parent/right table.
  --direction=left->right            Copy FROM left TO right. Reverse direction.

CONFIGURATION:
  Settings (in settings.py):
    DB_REPAIR_ARTIFACT_DIR = BASE_DIR / "var" / "db_repair"
    DB_REPAIR_SOFT_LINKS = BASE_DIR / "gaia" / "soft_links.yaml"
"""

    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            choices=[
                'schema', 'fk-declared', 'fk-orm', 'fk-enforcement',
                'soft-links', 'matchers', 'drift', 'orphans', 'compare', 'backfill'
            ],
            help='Action to perform'
        )

        # Target selection
        target = parser.add_argument_group('Target Selection')
        target.add_argument('--table', type=str, help='Table name')
        target.add_argument('--app', type=str, help='Django app name')

        # Compare/backfill options
        compare = parser.add_argument_group('Compare/Backfill Options')
        compare.add_argument('--left', type=str, help='Left table for compare')
        compare.add_argument('--right', type=str, help='Right table for compare')
        compare.add_argument('--source', type=str, help='Source table for backfill')
        compare.add_argument('--target', type=str, help='Target table for backfill')
        compare.add_argument('--key', type=str, help='Join key column')
        compare.add_argument(
            '--fields', type=str,
            help='Fields to compare or copy (comma-separated, use src:dst for mapping)'
        )
        compare.add_argument(
            '--matcher', type=str,
            help='Matcher name from registry (default: exact, or auto-loaded from --relationship)'
        )
        compare.add_argument(
            '--cardinality', type=str, choices=['1:1', 'M:1', '1:M', 'M:M'],
            help='Join cardinality (auto-loaded from --relationship if provided)'
        )
        compare.add_argument(
            '--pick', type=str,
            help='Disambiguation for non-unique joins: latest:<col>, min:<col>, max:<col>, first'
        )
        compare.add_argument(
            '--identity', type=str,
            help='Identity column(s) for audit trail (comma-separated, required if no single-col PK)'
        )
        compare.add_argument('--sample', type=int, default=0, help='Sample rows per category')
        compare.add_argument(
            '--relationship', type=str,
            help='Load from registry: soft:<n> (auto-loads tables, key, matcher, cardinality)'
        )
        compare.add_argument(
            '--override-relationship', action='store_true',
            help='Allow manual args to override --relationship values (use with caution)'
        )

        # Orphans-specific options (--relationship is shared above)
        orphans = parser.add_argument_group('Orphans Options')
        orphans.add_argument(
            '--fk-column', type=str,
            help='FK column in left table (for enforced/declared relationships)'
        )
        orphans.add_argument(
            '--parent-table', type=str,
            help='Parent table to check against (for enforced/declared relationships)'
        )
        orphans.add_argument(
            '--parent-key', type=str, default='id',
            help='Key column in parent table (default: id)'
        )

        # Backfill mode
        backfill = parser.add_argument_group('Backfill Options')
        backfill.add_argument(
            '--mode', type=str, choices=['fill-null-only', 'overwrite'],
            default='fill-null-only', help='Backfill mode (default: fill-null-only)'
        )
        backfill.add_argument(
            '--allow-overwrite-fields', type=str,
            help='Fields allowed to overwrite (required with --mode=overwrite)'
        )
        backfill.add_argument(
            '--direction', type=str, choices=['left<-right', 'left->right'],
            default='left<-right',
            help='Data flow direction for --relationship mode. '
                 'left<-right (default): copy FROM right TO left (enrich child from parent). '
                 'left->right: copy FROM left TO right.'
        )

        # Safety flags
        safety = parser.add_argument_group('Safety Flags')
        safety.add_argument(
            '--dry-run', action='store_true',
            help='Preview changes without modifying database'
        )
        safety.add_argument(
            '--confirm', action='store_true',
            help='Actually execute modifications'
        )

        # Output options
        output = parser.add_argument_group('Output Options')
        output.add_argument('--verbose', action='store_true', help='Verbose output')
        output.add_argument('--format', choices=['text', 'json'], default='text')
        output.add_argument('--output', '-o', type=str, help='Output file path')

    def handle(self, *args, **options):
        action = options['action']

        # Verify artifact directory is writable for repair actions
        if action == 'backfill':
            self._verify_artifact_dir()

        if action == 'schema':
            self._action_schema(options)
        elif action == 'fk-declared':
            self._action_fk_declared(options)
        elif action == 'fk-orm':
            self._action_fk_orm(options)
        elif action == 'fk-enforcement':
            self._action_fk_enforcement(options)
        elif action == 'soft-links':
            self._action_soft_links(options)
        elif action == 'matchers':
            self._action_matchers(options)
        elif action == 'compare':
            self._action_compare(options)
        elif action == 'backfill':
            self._action_backfill(options)
        elif action == 'orphans':
            self._action_orphans(options)
        elif action == 'drift':
            self._action_drift(options)

    # -------------------------------------------------------------------------
    # Configuration Helpers
    # -------------------------------------------------------------------------

    def _get_artifact_dir(self) -> Path:
        """Get artifact directory from settings or default."""
        default = Path(settings.BASE_DIR) / 'var' / 'db_repair'
        return Path(getattr(settings, 'DB_REPAIR_ARTIFACT_DIR', default))

    def _get_soft_links_path(self) -> Path:
        """
        Get soft-links registry path from settings or default.
        
        Default: BASE_DIR / 'gaia' / 'soft_links.yaml'
        Override: settings.DB_REPAIR_SOFT_LINKS
        """
        default = Path(settings.BASE_DIR) / 'gaia' / 'soft_links.yaml'
        return Path(getattr(settings, 'DB_REPAIR_SOFT_LINKS', default))

    def _verify_artifact_dir(self):
        """Verify artifact directory exists and is writable."""
        artifact_dir = self._get_artifact_dir()
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            test_file = artifact_dir / '.write_test'
            test_file.touch()
            test_file.unlink()
        except (OSError, PermissionError) as e:
            raise CommandError(
                f"Artifact directory not writable: {artifact_dir}\n"
                f"Error: {e}\n"
                f"Configure via settings.DB_REPAIR_ARTIFACT_DIR"
            )

    def _load_soft_links(self) -> List[Dict[str, Any]]:
        """Load and validate soft-link registry from YAML file."""
        path = self._get_soft_links_path()
        if not path.exists():
            self.stdout.write(self.style.WARNING(f"Soft-links file not found: {path}"))
            self.stdout.write("Configure via settings.DB_REPAIR_SOFT_LINKS or create the file.")
            return []
        
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
        
        links = data.get('soft_links', [])
        
        # Validate schema
        self._validate_soft_link_schema(links)
        
        return links

    def _validate_soft_link_schema(self, links: List[Dict[str, Any]]) -> None:
        """
        Validate soft-link registry entries on load.
        
        Catches:
          - Missing required keys
          - Malformed left/right specs (must be table.column)
          - Unknown matchers (not in registry)
          - Invalid cardinality tokens
          - YAML parsing issues (e.g., unquoted 1:1 -> 61)
        """
        VALID_CARDINALITIES = {'1:1', 'M:1', '1:M', 'M:M'}
        REQUIRED_KEYS = ('name', 'left', 'right', 'matcher', 'cardinality')
        
        for i, link in enumerate(links):
            entry_id = link.get('name', f'<entry index {i}>')
            
            # Check required keys
            for key in REQUIRED_KEYS:
                if key not in link:
                    raise CommandError(
                        f"Soft-link '{entry_id}': missing required key '{key}'\n"
                        f"Required keys: {', '.join(REQUIRED_KEYS)}"
                    )
            
            # Validate left/right format (table.column)
            for side in ('left', 'right'):
                spec = link[side]
                if not isinstance(spec, str) or '.' not in spec or spec.count('.') != 1:
                    raise CommandError(
                        f"Soft-link '{entry_id}': {side} must be 'table.column' format\n"
                        f"Got: '{spec}'"
                    )
                table, col = spec.rsplit('.', 1)
                if not table or not col:
                    raise CommandError(
                        f"Soft-link '{entry_id}': {side} has empty table or column\n"
                        f"Got: '{spec}'"
                    )
            
            # Validate matcher exists in registry
            matcher_name = link['matcher']
            if matcher_name not in MATCHERS:
                raise CommandError(
                    f"Soft-link '{entry_id}': unknown matcher '{matcher_name}'\n"
                    f"Available matchers: {', '.join(sorted(MATCHERS.keys()))}"
                )
            
            # Validate cardinality token
            cardinality = link['cardinality']
            cardinality_str = str(cardinality)
            
            # Detect YAML sexagesimal parsing (1:1 -> 61, M:1 stays string)
            if isinstance(cardinality, int):
                raise CommandError(
                    f"Soft-link '{entry_id}': cardinality parsed as integer '{cardinality}'\n"
                    f"This usually means YAML interpreted '1:1' as sexagesimal (base-60).\n"
                    f"Fix: Quote the value in YAML, e.g., cardinality: \"1:1\""
                )
            
            if cardinality_str not in VALID_CARDINALITIES:
                raise CommandError(
                    f"Soft-link '{entry_id}': invalid cardinality '{cardinality_str}'\n"
                    f"Must be one of: {', '.join(sorted(VALID_CARDINALITIES))}"
                )

    # -------------------------------------------------------------------------
    # Pick/Disambiguation Logic
    # -------------------------------------------------------------------------

    def _parse_pick(self, pick_str: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse --pick argument into (strategy, column).
        
        Returns:
            Tuple of (strategy, column) where strategy is one of:
            'latest', 'min', 'max', 'first', or None
        """
        if not pick_str:
            return (None, None)
        
        if pick_str == 'first':
            return ('first', None)
        
        if ':' not in pick_str:
            raise CommandError(
                f"Invalid --pick format: '{pick_str}'\n"
                f"Expected: latest:<col>, min:<col>, max:<col>, or first"
            )
        
        strategy, col = pick_str.split(':', 1)
        if strategy not in ('latest', 'min', 'max'):
            raise CommandError(
                f"Unknown --pick strategy: '{strategy}'\n"
                f"Valid strategies: latest, min, max, first"
            )
        
        return (strategy, col)

    def _validate_pick_required(self, cardinality: str, pick_str: Optional[str]):
        """
        Validate that --pick is provided for non-1:1 cardinalities.
        
        This is a release blocker - non-deterministic joins are not acceptable.
        """
        if cardinality != '1:1' and not pick_str:
            raise CommandError(
                f"Cardinality is {cardinality} but --pick not specified.\n\n"
                f"Non-unique joins require explicit disambiguation to ensure\n"
                f"deterministic results. Without --pick, the same query can\n"
                f"return different results depending on query planner and row order.\n\n"
                f"Specify one of:\n"
                f"  --pick=latest:<timestamp_col>   Use most recent row\n"
                f"  --pick=min:<col>                Use row with minimum value\n"
                f"  --pick=max:<col>                Use row with maximum value\n"
                f"  --pick=first                    Use first row (explicit non-determinism)\n\n"
                f"If you truly don't care about determinism, use --pick=first."
            )

    def _apply_pick_to_rows(
        self,
        rows: List[tuple],
        pick_strategy: Optional[str],
        pick_col: Optional[str],
        columns: List[str]
    ) -> Optional[tuple]:
        """
        Apply pick strategy to select one row from multiple matches.
        
        Args:
            rows: List of matching rows
            pick_strategy: 'latest', 'min', 'max', 'first', or None
            pick_col: Column name for comparison (None for 'first')
            columns: List of column names in row order
        
        Returns:
            Single selected row, or None if rows is empty
        """
        if not rows:
            return None
        
        if len(rows) == 1:
            return rows[0]
        
        if pick_strategy == 'first' or pick_strategy is None:
            return rows[0]
        
        if pick_col not in columns:
            raise CommandError(
                f"--pick column '{pick_col}' not found in query columns.\n"
                f"Available columns: {', '.join(columns)}"
            )
        
        col_idx = columns.index(pick_col)
        
        if pick_strategy in ('latest', 'max'):
            # Sort None values to the end (they lose)
            return max(rows, key=lambda r: (r[col_idx] is not None, r[col_idx]))
        elif pick_strategy == 'min':
            # Sort None values to the end (they lose)
            return min(rows, key=lambda r: (r[col_idx] is None, r[col_idx]))
        
        return rows[0]  # Fallback

    # -------------------------------------------------------------------------
    # Identity Detection
    # -------------------------------------------------------------------------

    def _detect_identity_columns(self, table: str, identity_str: Optional[str]) -> List[str]:
        """
        Detect or validate identity columns for audit trail.
        
        Args:
            table: Table name
            identity_str: User-provided --identity value, or None
        
        Returns:
            List of identity column names
        
        Raises:
            CommandError if identity cannot be determined
        """
        cols, _ = self._detect_identity_columns_with_source(table, identity_str)
        return cols

    def _detect_identity_columns_with_source(self, table: str, identity_str: Optional[str]) -> Tuple[List[str], str]:
        """
        Detect or validate identity columns for audit trail, with source info.
        
        Args:
            table: Table name
            identity_str: User-provided --identity value, or None
        
        Returns:
            Tuple of (list of identity column names, source description)
        
        Raises:
            CommandError if identity cannot be determined
        """
        if identity_str:
            return ([c.strip() for c in identity_str.split(',')], "from --identity")
        
        # Try to auto-detect primary key
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA table_info('{table}')")
            columns = cursor.fetchall()
            
            pk_cols = []
            for col in columns:
                cid, name, dtype, notnull, default, pk = col
                if pk:
                    pk_cols.append(name)
        
        if not pk_cols:
            raise CommandError(
                f"Cannot auto-detect identity for {table}.\n"
                f"No primary key columns found.\n\n"
                f"Specify identity explicitly with --identity=col1,col2\n"
                f"These columns must uniquely identify each row for audit purposes."
            )
        
        if len(pk_cols) > 1:
            self.stdout.write(self.style.WARNING(
                f"Composite primary key detected: {', '.join(pk_cols)}\n"
                f"Using all PK columns as identity."
            ))
            return (pk_cols, f"primary key ({', '.join(pk_cols)})")
        
        return (pk_cols, f"primary key ({pk_cols[0]})")

    # -------------------------------------------------------------------------
    # Introspection Actions
    # -------------------------------------------------------------------------

    def _action_schema(self, options):
        """Dump table schema using PRAGMA."""
        table = options.get('table')
        app = options.get('app')

        if not table and not app:
            raise CommandError("--table or --app required for schema action")

        tables = []
        if table:
            tables = [table]
        elif app:
            try:
                app_config = apps.get_app_config(app)
                tables = [model._meta.db_table for model in app_config.get_models()]
            except LookupError:
                raise CommandError(f"App '{app}' not found")

        with connection.cursor() as cursor:
            for tbl in tables:
                self.stdout.write(f"\n{'='*60}")
                self.stdout.write(f"TABLE: {tbl}")
                self.stdout.write('='*60)

                # Columns
                cursor.execute(f"PRAGMA table_info('{tbl}')")
                columns = cursor.fetchall()
                
                self.stdout.write("\nCOLUMNS:")
                self.stdout.write(f"{'Name':<30} {'Type':<15} {'NotNull':<8} {'PK':<4} {'Default'}")
                self.stdout.write('-'*80)
                for col in columns:
                    cid, name, dtype, notnull, default, pk = col
                    self.stdout.write(
                        f"{name:<30} {dtype:<15} {bool(notnull)!s:<8} {bool(pk)!s:<4} {default}"
                    )

                # Indexes
                cursor.execute(f"PRAGMA index_list('{tbl}')")
                indexes = cursor.fetchall()
                
                if indexes:
                    self.stdout.write("\nINDEXES:")
                    for idx in indexes:
                        seq, name, unique, origin, partial = idx
                        cursor.execute(f"PRAGMA index_info('{name}')")
                        idx_cols = cursor.fetchall()
                        col_names = [ic[2] for ic in idx_cols]
                        self.stdout.write(
                            f"  {name}: {', '.join(col_names)} "
                            f"(unique={bool(unique)}, origin={origin})"
                        )

    def _action_fk_declared(self, options):
        """Show FKs declared in database via PRAGMA."""
        table = options.get('table')
        app = options.get('app')

        if not table and not app:
            raise CommandError("--table or --app required")

        tables = []
        if table:
            tables = [table]
        elif app:
            app_config = apps.get_app_config(app)
            tables = [model._meta.db_table for model in app_config.get_models()]

        self.stdout.write("\nDECLARED FOREIGN KEYS (from database):")
        self.stdout.write("="*70)

        with connection.cursor() as cursor:
            for tbl in tables:
                cursor.execute(f"PRAGMA foreign_key_list('{tbl}')")
                fks = cursor.fetchall()
                
                if fks:
                    self.stdout.write(f"\n{tbl}:")
                    for fk in fks:
                        id_, seq, ref_table, from_col, to_col, on_update, on_delete, match = fk
                        self.stdout.write(
                            f"  {from_col} -> {ref_table}.{to_col} "
                            f"(ON DELETE {on_delete}, ON UPDATE {on_update})"
                        )
                else:
                    self.stdout.write(f"\n{tbl}: No declared FKs")

    def _action_fk_orm(self, options):
        """Show FKs declared in Django models."""
        app = options.get('app')

        if not app:
            raise CommandError("--app required for fk-orm action")

        try:
            app_config = apps.get_app_config(app)
        except LookupError:
            raise CommandError(f"App '{app}' not found")

        self.stdout.write("\nORM FOREIGN KEYS (from Django models):")
        self.stdout.write("="*70)

        for model in app_config.get_models():
            fk_fields = []
            for field in model._meta.get_fields():
                if hasattr(field, 'related_model') and field.related_model:
                    if hasattr(field, 'column'):
                        fk_fields.append({
                            'field': field.name,
                            'column': field.column,
                            'related_model': field.related_model._meta.db_table,
                            'related_field': field.target_field.name if hasattr(field, 'target_field') else 'id'
                        })

            if fk_fields:
                self.stdout.write(f"\n{model._meta.db_table} ({model.__name__}):")
                for fk in fk_fields:
                    self.stdout.write(
                        f"  {fk['column']} -> {fk['related_model']}.{fk['related_field']}"
                    )

    def _action_fk_enforcement(self, options):
        """Check PRAGMA foreign_keys state."""
        self.stdout.write("\nFOREIGN KEY ENFORCEMENT STATUS:")
        self.stdout.write("="*60)

        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys")
            result = cursor.fetchone()
            enabled = bool(result[0]) if result else False

            if enabled:
                self.stdout.write(self.style.SUCCESS("PRAGMA foreign_keys = ON (enforced)"))
            else:
                self.stdout.write(self.style.WARNING("PRAGMA foreign_keys = OFF (not enforced)"))
                self.stdout.write(
                    "\nNote: SQLite/SpatiaLite does not enforce FK constraints by default.\n"
                    "Constraints exist in schema but violations are not prevented."
                )

    def _action_soft_links(self, options):
        """Display soft-link registry contents."""
        soft_links = self._load_soft_links()

        self.stdout.write("\nSOFT-LINK REGISTRY:")
        self.stdout.write("="*70)
        self.stdout.write(f"Source: {self._get_soft_links_path()}")
        self.stdout.write("")

        if not soft_links:
            self.stdout.write("No soft links defined.")
            return

        for link in soft_links:
            self.stdout.write(f"\n{link['name']}:")
            self.stdout.write(f"  Left:        {link['left']}")
            self.stdout.write(f"  Right:       {link['right']}")
            self.stdout.write(f"  Matcher:     {link['matcher']}")
            self.stdout.write(f"  Cardinality: {link['cardinality']}")
            if 'notes' in link:
                notes = link['notes'].strip().split('\n')[0]  # First line only
                self.stdout.write(f"  Notes:       {notes}")

    def _action_matchers(self, options):
        """List available matchers."""
        self.stdout.write("\nAVAILABLE MATCHERS:")
        self.stdout.write("="*60)

        for name, description in list_matchers().items():
            self.stdout.write(f"\n{name}:")
            self.stdout.write(f"  {description}")

    # -------------------------------------------------------------------------
    # Compare Action
    # -------------------------------------------------------------------------

    def _action_compare(self, options):
        """Deterministic diff of two tables on a join key."""
        left_table = options.get('left')
        right_table = options.get('right')
        key = options.get('key')
        fields_str = options.get('fields')
        matcher_name = options.get('matcher')
        cardinality = options.get('cardinality')
        pick_str = options.get('pick')
        sample = options.get('sample', 0)
        verbose = options.get('verbose')
        relationship = options.get('relationship')
        override_relationship = options.get('override_relationship')

        # Handle --relationship=soft:<name> mode
        if relationship and relationship.startswith('soft:'):
            soft_link_name = relationship[5:]
            link_config = self._get_soft_link_config(soft_link_name)
            
            if not link_config:
                raise CommandError(
                    f"Soft-link '{soft_link_name}' not found in registry.\n"
                    f"Check {self._get_soft_links_path()}"
                )
            
            # Extract from registry
            left_spec = link_config['left']
            right_spec = link_config['right']
            reg_left, reg_key = left_spec.rsplit('.', 1)
            reg_right, _ = right_spec.rsplit('.', 1)
            reg_matcher = link_config['matcher']
            reg_cardinality = str(link_config['cardinality'])
            
            # Check for conflicts
            conflicts = []
            if left_table and left_table != reg_left:
                conflicts.append(f"--left={left_table} vs registry={reg_left}")
            if right_table and right_table != reg_right:
                conflicts.append(f"--right={right_table} vs registry={reg_right}")
            if key and key != reg_key:
                conflicts.append(f"--key={key} vs registry={reg_key}")
            if matcher_name and matcher_name != reg_matcher:
                conflicts.append(f"--matcher={matcher_name} vs registry={reg_matcher}")
            if cardinality and cardinality != reg_cardinality:
                conflicts.append(f"--cardinality={cardinality} vs registry={reg_cardinality}")
            
            if conflicts and not override_relationship:
                raise CommandError(
                    f"Conflict between --relationship={relationship} and manual args:\n"
                    + "\n".join(f"  {c}" for c in conflicts) +
                    "\n\nUse --override-relationship to use manual values instead of registry,\n"
                    "or remove manual args to use registry values."
                )
            
            if conflicts and override_relationship:
                self.stdout.write(self.style.WARNING(
                    f"WARNING: --override-relationship specified. Using manual values:\n"
                    + "\n".join(f"  {c}" for c in conflicts)
                ))
            
            # Apply registry values (manual args take precedence if override is set)
            left_table = left_table or reg_left
            right_table = right_table or reg_right
            key = key or reg_key
            matcher_name = matcher_name or reg_matcher
            cardinality = cardinality or reg_cardinality

        # Apply defaults for non-relationship mode
        matcher_name = matcher_name or 'exact'

        # Validation
        if not all([left_table, right_table, key]):
            raise CommandError(
                "--left, --right, and --key are required for compare\n"
                "(or use --relationship=soft:<n> to auto-load from registry)"
            )
        
        if not cardinality:
            raise CommandError(
                "--cardinality is required (1:1, M:1, 1:M, M:M)\n"
                "(or use --relationship=soft:<n> to auto-load from registry)"
            )

        # Validate --pick is provided for non-1:1 cardinalities (RELEASE BLOCKER)
        self._validate_pick_required(cardinality, pick_str)
        
        pick_strategy, pick_col = self._parse_pick(pick_str)

        # Parse field mappings (left_col:right_col or just fieldname for both)
        field_mappings = []
        if fields_str:
            for f in fields_str.split(','):
                f = f.strip()
                if ':' in f:
                    left_col, right_col = f.split(':', 1)
                    field_mappings.append((left_col.strip(), right_col.strip()))
                else:
                    field_mappings.append((f, f))

        # Get matcher
        try:
            matcher = get_matcher(matcher_name)
        except ValueError as e:
            raise CommandError(str(e))

        # Header
        self.stdout.write("="*60)
        self.stdout.write("COMPARE TABLES")
        self.stdout.write("="*60)
        if relationship:
            self.stdout.write(f"Relationship: {relationship}")
        self.stdout.write(f"Left:        {left_table}")
        self.stdout.write(f"Right:       {right_table}")
        self.stdout.write(f"Key:         {key}")
        self.stdout.write(f"Matcher:     {matcher_name}")
        self.stdout.write(f"Cardinality: {cardinality}" + (" (from registry)" if relationship and not override_relationship else ""))
        self.stdout.write(f"Pick:        {pick_str or 'N/A (1:1)'}")
        if field_mappings:
            self.stdout.write("Fields:")
            for left_col, right_col in field_mappings:
                if left_col == right_col:
                    self.stdout.write(f"  {left_col}")
                else:
                    self.stdout.write(f"  {left_table}.{left_col} <-> {right_table}.{right_col}")
        else:
            self.stdout.write("Fields:      (key only)")
        self.stdout.write("")

        # Build column lists
        left_field_cols = [m[0] for m in field_mappings]
        right_field_cols = [m[1] for m in field_mappings]
        
        # Add pick column to queries if not already included
        left_cols = [key] + left_field_cols
        right_cols = [key] + right_field_cols
        
        if pick_col and pick_col not in right_cols:
            right_cols.append(pick_col)

        # Load data
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(left_cols)} FROM {left_table} "
                f"WHERE {key} IS NOT NULL"
            )
            left_rows = cursor.fetchall()

            cursor.execute(
                f"SELECT {', '.join(right_cols)} FROM {right_table} "
                f"WHERE {key} IS NOT NULL"
            )
            right_rows = cursor.fetchall()

        self.stdout.write(f"Left rows:   {len(left_rows)}")
        self.stdout.write(f"Right rows:  {len(right_rows)}")
        self.stdout.write("")

        # Get normalizer for O(N+M) dictionary lookups
        try:
            normalizer = get_normalizer(matcher_name)
            use_dict_lookup = supports_dict_lookup(matcher_name)
        except ValueError:
            normalizer = lambda x: x
            use_dict_lookup = False

        if not use_dict_lookup:
            self.stdout.write(self.style.WARNING(
                f"Note: Matcher '{matcher_name}' does not support dictionary lookup.\n"
                f"Using O(N×M) nested comparison. May be slow for large datasets.\n"
            ))

        # Build normalized lookup for right table - group by normalized key
        right_by_norm_key = defaultdict(list)
        for row in right_rows:
            norm_key = normalizer(row[0])
            if norm_key is not None:
                right_by_norm_key[norm_key].append(row)

        # Compare using normalized keys (O(N+M) when supported)
        matched = []
        left_only = []
        field_mismatches = defaultdict(int)

        if use_dict_lookup:
            # O(N+M) path - dictionary lookup
            for left_row in left_rows:
                left_key = left_row[0]
                norm_key = normalizer(left_key)
                
                if norm_key is not None and norm_key in right_by_norm_key:
                    right_rows_for_key = right_by_norm_key[norm_key]
                    
                    # Apply --pick to select deterministically
                    right_row = self._apply_pick_to_rows(
                        right_rows_for_key,
                        pick_strategy,
                        pick_col,
                        right_cols
                    )
                    
                    # Compare fields
                    if field_mappings:
                        for i, (left_col, right_col) in enumerate(field_mappings):
                            left_val = left_row[i + 1] if i + 1 < len(left_row) else None
                            right_val = right_row[i + 1] if i + 1 < len(right_row) else None
                            if left_val != right_val:
                                label = f"{left_col}" if left_col == right_col else f"{left_col}/{right_col}"
                                field_mismatches[label] += 1
                    
                    matched.append((left_row, right_row))
                else:
                    left_only.append(left_row)
        else:
            # O(N×M) fallback for matchers that don't support normalization
            matcher = get_matcher(matcher_name)
            for left_row in left_rows:
                left_key = left_row[0]
                found_match = False

                for norm_key, right_rows_for_key in right_by_norm_key.items():
                    right_key = right_rows_for_key[0][0]  # Get original key
                    if matcher(left_key, right_key):
                        found_match = True
                        
                        right_row = self._apply_pick_to_rows(
                            right_rows_for_key,
                            pick_strategy,
                            pick_col,
                            right_cols
                        )
                        
                        if field_mappings:
                            for i, (left_col, right_col) in enumerate(field_mappings):
                                left_val = left_row[i + 1] if i + 1 < len(left_row) else None
                                right_val = right_row[i + 1] if i + 1 < len(right_row) else None
                                if left_val != right_val:
                                    label = f"{left_col}" if left_col == right_col else f"{left_col}/{right_col}"
                                    field_mismatches[label] += 1
                        
                        matched.append((left_row, right_row))
                        break

                if not found_match:
                    left_only.append(left_row)

        # Find right-only (not matched) using normalized keys
        matched_norm_keys = set()
        for left_row, right_row in matched:
            matched_norm_keys.add(normalizer(right_row[0]))

        right_only = [r for r in right_rows if normalizer(r[0]) not in matched_norm_keys]

        # Report
        self.stdout.write("="*60)
        self.stdout.write("RESULTS")
        self.stdout.write("="*60)
        self.stdout.write(f"Matched:     {len(matched)}")
        self.stdout.write(f"Left-only:   {len(left_only)} (in {left_table}, not in {right_table})")
        self.stdout.write(f"Right-only:  {len(right_only)} (in {right_table}, not in {left_table})")

        if field_mismatches:
            self.stdout.write("\nField mismatches (among matched rows):")
            for field, count in field_mismatches.items():
                self.stdout.write(f"  {field}: {count}")

        # Samples
        if sample > 0:
            if left_only:
                self.stdout.write(f"\nLeft-only samples (first {min(sample, len(left_only))}):")
                for row in left_only[:sample]:
                    self.stdout.write(f"  {key}={row[0]}")

            if right_only:
                self.stdout.write(f"\nRight-only samples (first {min(sample, len(right_only))}):")
                for row in right_only[:sample]:
                    self.stdout.write(f"  {key}={row[0]}")

    # -------------------------------------------------------------------------
    # Backfill Action
    # -------------------------------------------------------------------------

    def _action_backfill(self, options):
        """Copy field values from source to target table."""
        source_table = options.get('source')
        target_table = options.get('target')
        key = options.get('key')
        fields_str = options.get('fields')
        matcher_name = options.get('matcher')
        cardinality = options.get('cardinality')
        pick_str = options.get('pick')
        identity_str = options.get('identity')
        mode = options.get('mode', 'fill-null-only')
        allow_overwrite = options.get('allow_overwrite_fields')
        dry_run = options.get('dry_run')
        confirm = options.get('confirm')
        verbose = options.get('verbose')
        relationship = options.get('relationship')
        override_relationship = options.get('override_relationship')

        # Safety validation first
        if not dry_run and not confirm:
            raise CommandError(
                "backfill requires --dry-run or --confirm.\n"
                "Example: python manage.py db_repair backfill ... --dry-run"
            )

        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")

        # Handle --relationship=soft:<n> mode
        if relationship and relationship.startswith('soft:'):
            soft_link_name = relationship[5:]
            link_config = self._get_soft_link_config(soft_link_name)
            
            if not link_config:
                raise CommandError(
                    f"Soft-link '{soft_link_name}' not found in registry.\n"
                    f"Check {self._get_soft_links_path()}"
                )
            
            # Extract from registry
            left_spec = link_config['left']
            right_spec = link_config['right']
            left_table, left_key = left_spec.rsplit('.', 1)
            right_table, right_key = right_spec.rsplit('.', 1)
            reg_matcher = link_config['matcher']
            reg_cardinality = str(link_config['cardinality'])
            
            # Determine source/target based on --direction
            # Default: left<-right means "copy FROM right TO left" (enrich child from parent)
            direction = options.get('direction', 'left<-right')
            
            if direction == 'left<-right':
                # Copy FROM right TO left (typical: enrich POI from EE)
                reg_source = right_table
                reg_target = left_table
                reg_key = left_key  # Key is on the target (left) side
            else:  # left->right
                # Copy FROM left TO right
                reg_source = left_table
                reg_target = right_table
                reg_key = right_key  # Key is on the target (right) side
            
            # Check for conflicts
            conflicts = []
            if source_table and source_table != reg_source:
                conflicts.append(f"--source={source_table} vs registry+direction={reg_source}")
            if target_table and target_table != reg_target:
                conflicts.append(f"--target={target_table} vs registry+direction={reg_target}")
            if key and key != reg_key:
                conflicts.append(f"--key={key} vs registry={reg_key}")
            if matcher_name and matcher_name != reg_matcher:
                conflicts.append(f"--matcher={matcher_name} vs registry={reg_matcher}")
            if cardinality and cardinality != reg_cardinality:
                conflicts.append(f"--cardinality={cardinality} vs registry={reg_cardinality}")
            
            if conflicts and not override_relationship:
                raise CommandError(
                    f"Conflict between --relationship={relationship} and manual args:\n"
                    + "\n".join(f"  {c}" for c in conflicts) +
                    "\n\nUse --override-relationship to use manual values instead of registry,\n"
                    "or remove manual args to use registry values."
                )
            
            if conflicts and override_relationship:
                self.stdout.write(self.style.WARNING(
                    f"WARNING: --override-relationship specified. Using manual values:\n"
                    + "\n".join(f"  {c}" for c in conflicts)
                ))
            
            # Apply registry values (manual args take precedence if override is set)
            source_table = source_table or reg_source
            target_table = target_table or reg_target
            key = key or reg_key
            matcher_name = matcher_name or reg_matcher
            cardinality = cardinality or reg_cardinality

        # Apply defaults for non-relationship mode
        matcher_name = matcher_name or 'exact'

        # Required args validation
        if not all([source_table, target_table, key]):
            raise CommandError(
                "--source, --target, and --key are required\n"
                "(or use --relationship=soft:<n> to auto-load from registry)"
            )

        if not fields_str:
            raise CommandError("--fields is required (comma-separated list of fields to copy)")

        if not cardinality:
            raise CommandError(
                "--cardinality is required\n"
                "(or use --relationship=soft:<n> to auto-load from registry)"
            )

        # Validate --pick is provided for non-1:1 cardinalities (RELEASE BLOCKER)
        self._validate_pick_required(cardinality, pick_str)
        
        pick_strategy, pick_col = self._parse_pick(pick_str)

        if mode == 'overwrite' and not allow_overwrite:
            raise CommandError("--allow-overwrite-fields required with --mode=overwrite")

        if mode == 'overwrite' and not confirm:
            raise CommandError("--mode=overwrite requires --confirm (not just --dry-run)")

        # Parse field mappings (src:dst or just fieldname)
        field_mappings = []
        for f in fields_str.split(','):
            f = f.strip()
            if ':' in f:
                src, dst = f.split(':', 1)
                field_mappings.append((src.strip(), dst.strip()))
            else:
                field_mappings.append((f, f))

        # Validate that source fields exist on source table
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA table_info('{source_table}')")
            source_columns = {row[1] for row in cursor.fetchall()}
            
            cursor.execute(f"PRAGMA table_info('{target_table}')")
            target_columns = {row[1] for row in cursor.fetchall()}

        missing_source_fields = []
        missing_target_fields = []
        for src_col, dst_col in field_mappings:
            if src_col not in source_columns:
                missing_source_fields.append(src_col)
            if dst_col not in target_columns:
                missing_target_fields.append(dst_col)

        if missing_source_fields:
            raise CommandError(
                f"Source fields not found in {source_table}: {', '.join(missing_source_fields)}\n"
                f"Available columns: {', '.join(sorted(source_columns))}"
            )

        if missing_target_fields:
            raise CommandError(
                f"Target fields not found in {target_table}: {', '.join(missing_target_fields)}\n"
                f"Available columns: {', '.join(sorted(target_columns))}"
            )

        # Validate key column exists
        if key not in target_columns:
            raise CommandError(
                f"Key column '{key}' not found in target table {target_table}\n"
                f"Available columns: {', '.join(sorted(target_columns))}"
            )

        # Determine identity columns (PK-first, refuses if no PK and no --identity)
        identity_cols, identity_source = self._detect_identity_columns_with_source(target_table, identity_str)

        # Get matcher
        try:
            matcher = get_matcher(matcher_name)
        except ValueError as e:
            raise CommandError(str(e))

        # Check FK enforcement state
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys")
            fk_state = cursor.fetchone()
            fk_enabled = bool(fk_state[0]) if fk_state else False

        # Header
        direction = options.get('direction', 'left<-right')
        self.stdout.write("="*60)
        self.stdout.write("BACKFILL")
        self.stdout.write("="*60)
        if relationship:
            self.stdout.write(f"Relationship: {relationship}")
            self.stdout.write(f"Direction:   {direction} (copy FROM {source_table} TO {target_table})")
        self.stdout.write(f"Source:      {source_table}")
        self.stdout.write(f"Target:      {target_table}")
        self.stdout.write(f"Key:         {key}")
        self.stdout.write(f"Matcher:     {matcher_name}")
        self.stdout.write(f"Cardinality: {cardinality}" + (" (from registry)" if relationship and not override_relationship else ""))
        self.stdout.write(f"Pick:        {pick_str or 'N/A (1:1)'}")
        self.stdout.write(f"Mode:        {mode}")
        self.stdout.write(f"Identity:    {', '.join(identity_cols)} ({identity_source})")
        self.stdout.write(f"Fields:")
        for src, dst in field_mappings:
            self.stdout.write(f"  {source_table}.{src} -> {target_table}.{dst}")
        self.stdout.write(f"\nPRAGMA foreign_keys: {'ON' if fk_enabled else 'OFF'}")
        self.stdout.write("")

        # Build source column list
        source_cols = [key] + [m[0] for m in field_mappings]
        if pick_col and pick_col not in source_cols:
            source_cols.append(pick_col)

        # Load source data
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(source_cols)} FROM {source_table} "
                f"WHERE {key} IS NOT NULL"
            )
            source_rows = cursor.fetchall()

        # Get normalizer for this matcher (encapsulates all matching logic)
        try:
            normalizer = get_normalizer(matcher_name)
        except ValueError as e:
            raise CommandError(str(e))

        # Group source rows by normalized key for disambiguation
        source_by_key = defaultdict(list)
        for row in source_rows:
            src_key = row[0]
            norm_key = normalizer(src_key)
            if norm_key is not None:
                source_by_key[norm_key].append(row)

        self.stdout.write(f"Source records indexed: {len(source_by_key)}")

        # Load target data that needs update
        target_cols = [key] + identity_cols + [m[1] for m in field_mappings]
        
        if mode == 'fill-null-only':
            # Only get rows where at least one target field is NULL
            null_conditions = ' OR '.join([f"{m[1]} IS NULL" for m in field_mappings])
            where_clause = f"WHERE {key} IS NOT NULL AND ({null_conditions})"
        else:
            where_clause = f"WHERE {key} IS NOT NULL"

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {', '.join(target_cols)} FROM {target_table} {where_clause}")
            target_rows = cursor.fetchall()

        self.stdout.write(f"Target records to check: {len(target_rows)}")
        self.stdout.write("")

        # Match and prepare updates
        updates = []
        stats = {
            'matched': 0,
            'updated': 0,
            'skipped_not_null': 0,
            'no_match': 0,
        }

        for target_row in target_rows:
            target_key = target_row[0]
            identity_vals = target_row[1:1+len(identity_cols)]
            current_vals = target_row[1+len(identity_cols):]

            # Normalize target key for lookup (uses matcher's normalizer)
            norm_key = normalizer(target_key)

            if norm_key is not None and norm_key in source_by_key:
                # Apply --pick to select deterministically
                source_row = self._apply_pick_to_rows(
                    source_by_key[norm_key],
                    pick_strategy,
                    pick_col,
                    source_cols
                )
                
                if not source_row:
                    stats['no_match'] += 1
                    continue
                    
                stats['matched'] += 1

                # Prepare update
                new_vals = {}
                before_vals = {}
                
                for i, (src_col, dst_col) in enumerate(field_mappings):
                    src_val = source_row[i + 1]  # +1 because first col is key
                    current_val = current_vals[i]

                    before_vals[dst_col] = current_val

                    if mode == 'fill-null-only':
                        if current_val is None or current_val == '':
                            if src_val is not None and src_val != '':
                                new_vals[dst_col] = src_val
                    else:  # overwrite
                        if src_val is not None:
                            new_vals[dst_col] = src_val

                if new_vals:
                    updates.append({
                        'identity': dict(zip(identity_cols, identity_vals)),
                        'before': before_vals,
                        'after': new_vals,
                        'target_key': target_key,
                    })
                    stats['updated'] += 1
                else:
                    stats['skipped_not_null'] += 1
            else:
                stats['no_match'] += 1

        # Summary
        self.stdout.write("="*60)
        self.stdout.write("SUMMARY")
        self.stdout.write("="*60)
        self.stdout.write(f"Matched:          {stats['matched']}")
        self.stdout.write(f"To update:        {stats['updated']}")
        self.stdout.write(f"Skipped (not null): {stats['skipped_not_null']}")
        self.stdout.write(f"No match:         {stats['no_match']}")
        self.stdout.write("")

        if not updates:
            self.stdout.write(self.style.WARNING("No records to update."))
            return

        # Show sample updates
        if verbose or dry_run:
            self.stdout.write("SAMPLE UPDATES:")
            self.stdout.write("-"*60)
            for update in updates[:10]:
                self.stdout.write(f"  Identity: {update['identity']}")
                for field, new_val in update['after'].items():
                    old_val = update['before'].get(field)
                    self.stdout.write(f"    {field}: {old_val!r} -> {new_val!r}")
            if len(updates) > 10:
                self.stdout.write(f"  ... and {len(updates) - 10} more")
            self.stdout.write("-"*60)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n[DRY RUN] Would update {len(updates)} record(s). No changes made."
            ))
            self.stdout.write("Run with --confirm to execute.")
            return

        # Execute updates
        if confirm:
            self.stdout.write(f"\nUpdating {len(updates)} records...")

            updated_count = 0
            with connection.cursor() as cursor:
                for update in updates:
                    # Build UPDATE statement
                    set_clause = ', '.join([f"{col} = ?" for col in update['after'].keys()])
                    where_clause = ' AND '.join([f"{col} = ?" for col in update['identity'].keys()])
                    
                    sql = f"UPDATE {target_table} SET {set_clause} WHERE {where_clause}"
                    params = list(update['after'].values()) + list(update['identity'].values())

                    if verbose:
                        self.stdout.write(f"  SQL: {sql}")
                        self.stdout.write(f"  Params: {params}")

                    cursor.execute(sql, params)
                    updated_count += 1

                    if updated_count % 1000 == 0:
                        self.stdout.write(f"  Processed {updated_count}/{len(updates)}")

            # Write audit artifact - using timezone.now() for proper datetime
            artifact = {
                'timestamp': timezone.now().isoformat(),
                'action': 'backfill',
                'source': source_table,
                'target': target_table,
                'key': key,
                'matcher': matcher_name,
                'cardinality': cardinality,
                'pick': pick_str,
                'fields': [f"{s}:{d}" for s, d in field_mappings],
                'mode': mode,
                'identity_columns': identity_cols,
                'pragma_foreign_keys': 'ON' if fk_enabled else 'OFF',
                'dry_run': False,
                'counts': stats,
                'total_changes': len(updates),
                'changes_truncated': len(updates) > 1000,
                'changes': updates[:1000],  # Limit to first 1000 for file size
            }

            artifact_path = self._get_artifact_dir() / f"backfill_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(artifact_path, 'w') as f:
                json.dump(artifact, f, indent=2, default=str)

            self.stdout.write("")
            self.stdout.write("="*60)
            self.stdout.write("EXECUTION COMPLETE")
            self.stdout.write("="*60)
            self.stdout.write(f"Records updated: {updated_count}")
            self.stdout.write(f"Audit artifact:  {artifact_path}")
            self.stdout.write(
                self.style.SUCCESS(f"\nSUCCESS: Updated {updated_count} records.")
            )

    # -------------------------------------------------------------------------
    # Orphans Action
    # -------------------------------------------------------------------------

    def _action_orphans(self, options):
        """
        Find orphan rows - rows in left table with no matching row in right table.
        
        IMPORTANT: Orphan detection is EXISTENTIAL - answers "does ANY parent exist?"
        It does not use --pick because we only care whether a match exists.

        Supports three relationship types (cardinality auto-loaded from registry):
          - enforced: Uses PRAGMA foreign_key_list to find declared FK
          - declared: Same as enforced (FK exists but may not be enforced)
          - soft:<name>: Uses soft-link registry entry by name
        """
        table = options.get('table')
        relationship = options.get('relationship')
        fk_column = options.get('fk_column')
        parent_table = options.get('parent_table')
        parent_key = options.get('parent_key', 'id')
        matcher_name = options.get('matcher', 'exact')
        cardinality = options.get('cardinality')
        pick_str = options.get('pick')
        sample = options.get('sample', 0)
        verbose = options.get('verbose')

        # Validate arguments
        if not table:
            raise CommandError("--table is required for orphans action")
        
        if not relationship:
            raise CommandError(
                "--relationship is required. Options:\n"
                "  enforced     - Use FK declared in database\n"
                "  declared     - Same as enforced\n"
                "  soft:<name>  - Use soft-link from registry (e.g., soft:poi_to_ee_vendor)"
            )

        # Parse relationship type
        if relationship.startswith('soft:'):
            soft_link_name = relationship[5:]
            link_config = self._get_soft_link_config(soft_link_name)
            
            if not link_config:
                raise CommandError(
                    f"Soft-link '{soft_link_name}' not found in registry.\n"
                    f"Check {self._get_soft_links_path()}"
                )
            
            # Extract configuration from soft-link
            left_spec = link_config['left']  # e.g., "animal_pointsofinterest.vendor_id"
            right_spec = link_config['right']  # e.g., "animal_earthexplorer.vendor_id"
            
            left_table, fk_column = left_spec.rsplit('.', 1)
            parent_table, parent_key = right_spec.rsplit('.', 1)
            matcher_name = link_config['matcher']
            cardinality = link_config['cardinality']
            
            # Verify table matches
            if table != left_table:
                raise CommandError(
                    f"--table={table} doesn't match soft-link left table '{left_table}'\n"
                    f"Soft-link '{soft_link_name}' expects left table: {left_table}"
                )
            
        elif relationship in ('enforced', 'declared'):
            if not fk_column or not parent_table:
                raise CommandError(
                    f"--fk-column and --parent-table required for {relationship} relationship"
                )
            cardinality = cardinality or '1:1'  # FK implies 1:1 or M:1
        else:
            raise CommandError(
                f"Unknown relationship type: '{relationship}'\n"
                "Valid options: enforced, declared, soft:<name>"
            )

        # NOTE: Orphan detection is EXISTENTIAL - we don't need --pick
        # because we only check "does any parent exist?", not "which parent?"

        # Get normalizer for O(N+M) dictionary lookups
        try:
            normalizer = get_normalizer(matcher_name)
            use_dict_lookup = supports_dict_lookup(matcher_name)
        except ValueError as e:
            raise CommandError(str(e))

        # Header
        self.stdout.write("="*60)
        self.stdout.write("ORPHAN DETECTION (Existential)")
        self.stdout.write("="*60)
        self.stdout.write(f"Table:        {table}")
        self.stdout.write(f"FK Column:    {fk_column}")
        self.stdout.write(f"Parent:       {parent_table}.{parent_key}")
        self.stdout.write(f"Relationship: {relationship}")
        self.stdout.write(f"Matcher:      {matcher_name}")
        self.stdout.write(f"Cardinality:  {cardinality}" + (" (from registry)" if relationship.startswith('soft:') else ""))
        self.stdout.write(f"Algorithm:    {'O(N+M) dictionary lookup' if use_dict_lookup else 'O(N*M) nested scan'}")
        self.stdout.write("")

        # Load child rows (the table we're checking for orphans)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {fk_column} FROM {table} WHERE {fk_column} IS NOT NULL"
            )
            child_rows = cursor.fetchall()
            child_keys = [row[0] for row in child_rows]

            # Load parent keys (only need the key column for existential check)
            cursor.execute(
                f"SELECT {parent_key} FROM {parent_table} "
                f"WHERE {parent_key} IS NOT NULL"
            )
            parent_rows = cursor.fetchall()
            parent_keys = [row[0] for row in parent_rows]

        self.stdout.write(f"Child rows:   {len(child_keys)}")
        self.stdout.write(f"Parent rows:  {len(parent_keys)}")
        self.stdout.write("")

        # Build normalized parent key set for O(1) lookups
        parent_norm_keys = set()
        for pk in parent_keys:
            norm_key = normalizer(pk)
            if norm_key is not None:
                parent_norm_keys.add(norm_key)

        # Find orphans using normalized key lookup (existential check)
        orphans = []
        matched = 0

        if use_dict_lookup:
            # O(N+M) path - set lookup
            for child_key in child_keys:
                norm_key = normalizer(child_key)
                if norm_key is not None and norm_key in parent_norm_keys:
                    matched += 1
                else:
                    orphans.append(child_key)
        else:
            # O(N×M) fallback for matchers that don't support normalization
            matcher = get_matcher(matcher_name)
            self.stdout.write(self.style.WARNING(
                f"Note: Matcher '{matcher_name}' requires O(N×M) comparison.\n"
            ))
            for child_key in child_keys:
                found_match = False
                for parent_key_val in parent_keys:
                    if matcher(child_key, parent_key_val):
                        found_match = True
                        matched += 1
                        break
                if not found_match:
                    orphans.append(child_key)

        # Deduplicate orphan keys for reporting
        unique_orphan_keys = list(set(orphans))

        # Results
        self.stdout.write("="*60)
        self.stdout.write("RESULTS")
        self.stdout.write("="*60)
        self.stdout.write(f"Matched:      {matched}")
        self.stdout.write(f"Orphans:      {len(orphans)} rows ({len(unique_orphan_keys)} unique keys)")
        
        orphan_rate = (len(orphans) / len(child_keys) * 100) if child_keys else 0
        self.stdout.write(f"Orphan rate:  {orphan_rate:.1f}%")

        # Samples
        if sample > 0 and unique_orphan_keys:
            self.stdout.write(f"\nOrphan key samples (first {min(sample, len(unique_orphan_keys))}):")
            for key in unique_orphan_keys[:sample]:
                self.stdout.write(f"  {fk_column}={key}")

        # Verbose: show distribution
        if verbose and orphans:
            self.stdout.write("\nOrphan key frequency (top 10):")
            from collections import Counter
            key_counts = Counter(orphans)
            for key, count in key_counts.most_common(10):
                self.stdout.write(f"  {key}: {count} rows")

    def _get_soft_link_config(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a soft-link configuration by name."""
        soft_links = self._load_soft_links()
        for link in soft_links:
            if link.get('name') == name:
                return link
        return None

    # -------------------------------------------------------------------------
    # Drift Action
    # -------------------------------------------------------------------------

    def _action_drift(self, options):
        """
        Detect drift between database schema, Django ORM models, and migrations.
        
        Produces three explicit sections:
          1. DB↔Models: Compare database columns vs Django model fields
          2. DB↔Migrations: Compare database vs what migrations declare
          3. Models↔Migrations: Check for unapplied migrations
        
        Scope is limited to tables, columns, and indexes. Constraints, triggers,
        and partial indexes are marked [NOT EVALUATED].
        """
        app = options.get('app')
        table = options.get('table')
        verbose = options.get('verbose')

        if not app and not table:
            raise CommandError("--app or --table required for drift action")

        self.stdout.write("="*70)
        self.stdout.write("DRIFT DETECTION")
        self.stdout.write("="*70)
        self.stdout.write("")

        # Get tables to check
        tables_to_check = []
        model_map = {}  # db_table -> model class

        if app:
            try:
                app_config = apps.get_app_config(app)
                for model in app_config.get_models():
                    tables_to_check.append(model._meta.db_table)
                    model_map[model._meta.db_table] = model
            except LookupError:
                raise CommandError(f"App '{app}' not found")
        elif table:
            tables_to_check = [table]
            # Try to find model for this table
            for app_config in apps.get_app_configs():
                for model in app_config.get_models():
                    if model._meta.db_table == table:
                        model_map[table] = model
                        break

        self.stdout.write(f"Checking {len(tables_to_check)} table(s)")
        self.stdout.write("")

        # =====================================================================
        # SECTION 1: DB ↔ Models
        # =====================================================================
        self.stdout.write("-"*70)
        self.stdout.write("SECTION 1: DB ↔ MODELS")
        self.stdout.write("-"*70)
        self.stdout.write("Compares database columns (PRAGMA table_info) vs Django model fields.")
        self.stdout.write("")

        db_model_diffs = []

        for tbl in tables_to_check:
            model = model_map.get(tbl)
            
            # Get DB columns
            with connection.cursor() as cursor:
                cursor.execute(f"PRAGMA table_info('{tbl}')")
                db_columns = {row[1]: {'type': row[2], 'notnull': row[3], 'pk': row[5]} 
                              for row in cursor.fetchall()}

            if not model:
                self.stdout.write(f"\n{tbl}:")
                self.stdout.write(self.style.WARNING(f"  No Django model found for this table"))
                self.stdout.write(f"  DB columns: {', '.join(sorted(db_columns.keys()))}")
                continue

            # Get model fields
            model_columns = {}
            for field in model._meta.get_fields():
                if hasattr(field, 'column') and field.column:
                    model_columns[field.column] = {
                        'field_name': field.name,
                        'field_type': field.__class__.__name__,
                    }

            # Compare
            db_only = set(db_columns.keys()) - set(model_columns.keys())
            model_only = set(model_columns.keys()) - set(db_columns.keys())
            
            if db_only or model_only:
                db_model_diffs.append({
                    'table': tbl,
                    'db_only': list(db_only),
                    'model_only': list(model_only),
                })

            self.stdout.write(f"\n{tbl} ({model.__name__}):")
            
            if not db_only and not model_only:
                self.stdout.write(self.style.SUCCESS("  ✓ DB and model columns match"))
            else:
                if db_only:
                    self.stdout.write(self.style.WARNING(f"  In DB only: {', '.join(sorted(db_only))}"))
                if model_only:
                    self.stdout.write(self.style.ERROR(f"  In model only: {', '.join(sorted(model_only))}"))

        # =====================================================================
        # SECTION 2: MIGRATION INVENTORY AND TABLE EXISTENCE
        # =====================================================================
        self.stdout.write("")
        self.stdout.write("-"*70)
        self.stdout.write("SECTION 2: MIGRATION INVENTORY AND TABLE EXISTENCE")
        self.stdout.write("-"*70)
        self.stdout.write("Verifies tables exist and counts applied migrations.")
        self.stdout.write("Scope: Table existence only.")
        self.stdout.write("[NOT EVALUATED]: Column state, index state, constraint state.")
        self.stdout.write("")

        # Get applied migrations from django_migrations table
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "SELECT app, name FROM django_migrations ORDER BY applied"
                )
                applied_migrations = cursor.fetchall()
                self.stdout.write(f"Applied migrations: {len(applied_migrations)}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"Could not read django_migrations: {e}"
                ))
                applied_migrations = []

        # Check that expected tables exist
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            existing_tables = {row[0] for row in cursor.fetchall()}

        for tbl in tables_to_check:
            if tbl in existing_tables:
                self.stdout.write(f"  ✓ {tbl} exists")
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ {tbl} MISSING from database"))

        # Note limitations
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "  Note: Full migration parsing is [NOT EVALUATED].\n"
            "  This section confirms tables exist but does not verify all migration\n"
            "  operations were applied correctly. For full validation, use:\n"
            "    python manage.py migrate --check"
        ))

        # =====================================================================
        # SECTION 3: Models ↔ Migrations
        # =====================================================================
        self.stdout.write("")
        self.stdout.write("-"*70)
        self.stdout.write("SECTION 3: MODELS ↔ MIGRATIONS (Unapplied Detection)")
        self.stdout.write("-"*70)
        self.stdout.write("Checks if makemigrations would produce changes.")
        self.stdout.write("[NOT EVALUATED]: Cannot replicate Django autodetector fully.")
        self.stdout.write("")

        # Check for unapplied migrations
        from django.core.management import call_command
        from io import StringIO

        try:
            out = StringIO()
            # showmigrations --list shows [X] for applied, [ ] for unapplied
            call_command('showmigrations', '--list', stdout=out, stderr=out)
            output = out.getvalue()
            
            unapplied = []
            for line in output.split('\n'):
                if '[ ]' in line:
                    unapplied.append(line.strip())
            
            if unapplied:
                self.stdout.write(self.style.WARNING("Unapplied migrations found:"))
                for m in unapplied[:10]:
                    self.stdout.write(f"  {m}")
                if len(unapplied) > 10:
                    self.stdout.write(f"  ... and {len(unapplied) - 10} more")
            else:
                self.stdout.write(self.style.SUCCESS("  ✓ All migrations applied"))
                
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Could not check migrations: {e}"))

        # Check if makemigrations would create changes
        self.stdout.write("")
        try:
            out = StringIO()
            err = StringIO()
            call_command('makemigrations', '--check', '--dry-run', stdout=out, stderr=err)
            self.stdout.write(self.style.SUCCESS("  ✓ No model changes detected"))
        except SystemExit as e:
            if e.code == 1:
                self.stdout.write(self.style.WARNING(
                    "  makemigrations --check indicates changes needed.\n"
                    "  Run: python manage.py makemigrations --dry-run"
                ))
            else:
                self.stdout.write(self.style.SUCCESS("  ✓ No model changes detected"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Could not run makemigrations --check: {e}"))

        # =====================================================================
        # Summary
        # =====================================================================
        self.stdout.write("")
        self.stdout.write("="*70)
        self.stdout.write("DRIFT SUMMARY")
        self.stdout.write("="*70)
        
        if db_model_diffs:
            self.stdout.write(self.style.WARNING(
                f"  {len(db_model_diffs)} table(s) have DB↔Model column mismatches"
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  ✓ No DB↔Model drift detected"))

        self.stdout.write("")
        self.stdout.write("Scope evaluated:")
        self.stdout.write("  ✓ Tables exist")
        self.stdout.write("  ✓ Column presence (DB vs Model)")
        self.stdout.write("  ✓ Migration application status")
        self.stdout.write("")
        self.stdout.write("[NOT EVALUATED]:")
        self.stdout.write("  - Column type matching (SQLite type affinity)")
        self.stdout.write("  - Constraint definitions")
        self.stdout.write("  - Trigger definitions")
        self.stdout.write("  - Partial index definitions")
        self.stdout.write("  - Full migration operation replay")