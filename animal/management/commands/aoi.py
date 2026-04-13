# ------------------------------------------------------------------------------
# ----- aoi.py -----------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Unified management command for AreaOfInterest operations.
#              Supports listing, deleting, and inspecting AOIs with flexible
#              filtering and safety mechanisms.
#
#    tickets:  GAIFAGP-406 (purge AOIs for revised annotation AOIs)
#              GAIFAGP-516 (bare except cleanup)
#              GAIFAGP-565 (fix mask_applied field name mismatch)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - AOIs are replaceable reference data, not permanent records
#      - AOIs are used to scope imagery loading and fishnet filtering
#      - Deleting AOIs may orphan related EarthExplorer/GEGD/MGP/Tasking records
#        (these use DO_NOTHING on delete, so FK integrity is preserved)
#
#    usage:    python manage.py aoi --help
#              python manage.py aoi list
#              python manage.py aoi list --format=table
#              python manage.py aoi load --input-dir=/path/to/geojsons --dry-run
#              python manage.py aoi load --input-dir=/path/to/geojsons --confirm
#              python manage.py aoi delete --all --dry-run
#              python manage.py aoi delete --all --confirm --i-really-want-to-delete-all
#              python manage.py aoi delete --id=132 --confirm
#              python manage.py aoi delete --name="Gulf of Mexico" --confirm
#              python manage.py aoi delete --filter="sqkm__gt=1000" --dry-run
#              python manage.py aoi inspect --id=132
#
# ------------------------------------------------------------------------------

import argparse
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from animal.models import AreaOfInterest, EarthExplorer, Tasking

# Optional imports for GEGD and MGP - graceful degradation if not available
try:
    from animal.models import GEOINTDiscovery
    HAS_GEGD = True
except ImportError:
    HAS_GEGD = False

try:
    from animal.models import MaxarGeospatialPlatform
    HAS_MGP = True
except ImportError:
    HAS_MGP = False

# Optional imports for load action - graceful degradation if not available
try:
    import geopandas as gpd
    from shapely.geometry import shape
    from pyproj import CRS
    HAS_GEO_LIBS = True
except ImportError:
    HAS_GEO_LIBS = False

from animal.utils.utils import TeeWriter


class Command(BaseCommand):
    help = """
Manage AreaOfInterest records in the GAIA database.

SOURCE OF TRUTH:
  AOIs are replaceable reference data used to scope imagery loading
  and fishnet filtering. They can be purged and reloaded as annotation
  requirements evolve.

ACTIONS:
  list      List AOIs with optional filtering
  load      Load AOIs from GeoJSON files
  delete    Delete AOIs (requires --dry-run or --confirm)
  inspect   Show detailed information for a specific AOI (with imagery summary)
  count     Count AOIs matching criteria
  audit     Audit AOIs showing imagery record counts (EE, GEGD, MGP)
  export    Export AOIs to GeoJSON file

EXAMPLES:
  # List all AOIs
  python manage.py aoi list

  # List AOIs in table format
  python manage.py aoi list --format=table

  # Load AOIs from a directory of GeoJSON files
  python manage.py aoi load --input-dir=/path/to/geojsons --dry-run
  python manage.py aoi load --input-dir=/path/to/geojsons --confirm

  # Load a single GeoJSON file
  python manage.py aoi load --file=/path/to/aoi.geojson --confirm

  # Preview deleting ALL AOIs
  python manage.py aoi delete --all --dry-run

  # Actually delete all AOIs (requires nuclear option flag)
  python manage.py aoi delete --all --confirm --i-really-want-to-delete-all

  # Delete a specific AOI by ID
  python manage.py aoi delete --id=132 --confirm

  # Delete AOIs by name (partial match)
  python manage.py aoi delete --name="Gulf" --dry-run

  # Delete AOIs larger than 1000 sq km
  python manage.py aoi delete --filter="sqkm__gt=1000" --dry-run

  # Inspect a specific AOI
  python manage.py aoi inspect --id=132

  # Count AOIs matching criteria
  python manage.py aoi count --filter="sqkm__lt=500"

  # Audit all AOIs (shows EE, GEGD, MGP counts)
  python manage.py aoi audit

  # Audit showing only empty AOIs
  python manage.py aoi audit --empty-only

GEOJSON SCHEMA:
  Required fields:
    - feature_id:       AOI ID (integer) - Links to EarthExplorer records
    - name:             Human-readable label for dropdowns/display
    - Shape:            Polygon geometry (automatic)
  
  Optional fields:
    - aoi_type:         "annotation" or "tasking" (future use)
    - mask_applied: land/water/none per DL-016 (also reads landmask_applied for backward compat)
    - Shape_Area:       Area in sq meters (auto-calculated if missing)
  
  Ignored fields:
    - OBJECTID:         ArcGIS internal ID (not used)
    - Shape_Length:     Perimeter (informational only)
  
  Filename convention: "{feature_id}.geojson" (e.g., "0135.geojson")
  
  CRS: Files in any CRS (e.g., EPSG:3338) are auto-reprojected to WGS84.
  
  Empty files (no features) are skipped with a warning.

PRESERVING FK RELATIONSHIPS:
  The --preserve-ids flag attempts to keep AOI IDs from filenames.
  This is useful when replacing AOIs that have related EarthExplorer records.
  
  Example: If you delete AOI id=6 (Cape Cod Bay) and reload with --preserve-ids,
  the new AOI gets id=6, and existing EE records remain linked.
  
  Without --preserve-ids, new AOIs get auto-assigned IDs, and any EE records
  pointing to old AOI IDs become "orphaned" (FK points to non-existent AOI).

RELATED RECORDS:
  EarthExplorer, GEOINTDiscovery, MaxarGeospatialPlatform, and Tasking models 
  have FKs to AOI with on_delete=DO_NOTHING. Deleting AOIs will NOT cascade-delete 
  related records, but those records will have orphaned aoi_id references. 
  The inspect and audit actions show related record counts.
"""

    def create_parser(self, prog_name, subcommand, **kwargs):
        """Use RawDescriptionHelpFormatter to preserve example formatting."""
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser):
        # Positional: action
        parser.add_argument(
            "action",
            choices=["list", "load", "delete", "inspect", "count", "audit", "export"],
            help="Action to perform: list, load, delete, inspect, count, or audit"
        )

        # Selection criteria (mutually supportive, not exclusive)
        selection = parser.add_argument_group("Selection Criteria")
        selection.add_argument(
            "--all",
            action="store_true",
            help="Select ALL records (required for bulk delete)"
        )
        selection.add_argument(
            "--id",
            type=int,
            dest="aoi_id",
            help="Select by primary key ID"
        )
        selection.add_argument(
            "--ids",
            type=str,
            help="Select by comma-separated IDs (e.g., --ids=132,133,134)"
        )
        selection.add_argument(
            "--name",
            type=str,
            help="Select by name (case-insensitive partial match)"
        )
        selection.add_argument(
            "--filter",
            type=str,
            action="append",
            dest="filters",
            help="Django ORM filter (e.g., --filter='sqkm__gt=1000'). Can be repeated."
        )

        # Load action options
        load_opts = parser.add_argument_group("Load Options (for load action)")
        load_opts.add_argument(
            "--input-dir",
            type=str,
            help="Directory containing GeoJSON files to load"
        )
        load_opts.add_argument(
            "--file",
            type=str,
            dest="input_file",
            help="Single GeoJSON file to load"
        )
        load_opts.add_argument(
            "--skip-duplicates",
            action="store_true",
            help="Skip files where AOI with same name already exists (default: error)"
        )
        load_opts.add_argument(
            "--replace-duplicates",
            action="store_true",
            help="Replace existing AOIs with same name (default: error)"
        )
        load_opts.add_argument(
            "--preserve-ids",
            action="store_true",
            help="Preserve AOI IDs from filename (e.g., 0132_FFS_LM.geojson -> id=132). "
                 "Useful for maintaining FK relationships with EarthExplorer records."
        )

        # Safety flags
        safety = parser.add_argument_group("Safety Flags (required for delete)")
        safety.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without modifying the database"
        )
        safety.add_argument(
            "--confirm",
            action="store_true",
            help="Actually execute destructive operations"
        )
        safety.add_argument(
            "--i-really-want-to-delete-all",
            action="store_true",
            dest="nuclear",
            help="Additional confirmation required for --all deletes (prevents accidents)"
        )

        # Output formatting
        output = parser.add_argument_group("Output Options")
        output.add_argument(
            "--format",
            choices=["simple", "table", "csv", "json", "jira"],
            default="simple",
            help="Output format for list action (default: simple)"
        )
        output.add_argument(
            "--quiet", "-q",
            action="store_true",
            help="Suppress non-essential output"
        )
        output.add_argument(
            "--output", "-o",
            type=str,
            dest="output_file",
            help="Write output to file (in addition to console). Use with --quiet for file-only."
        )

        # Audit action options
        audit_opts = parser.add_argument_group("Audit Options (for audit action)")
        audit_opts.add_argument(
            "--empty-only",
            action="store_true",
            help="Show only AOIs with no associated imagery records"
        )
        audit_opts.add_argument(
            "--delete-empty",
            action="store_true",
            help="Delete AOIs with no associated records (requires --confirm)"
        )

        # Export action options
        export_opts = parser.add_argument_group("Export Options (for export action)")
        export_opts.add_argument(
            "--export-file",
            type=str,
            help="Output file path for GeoJSON export (e.g., --export-file=aois.geojson)"
        )

    def handle(self, *args, **options):
        # Set up output redirection if --output specified
        output_file = None
        original_stdout = self.stdout
        
        if options.get("output_file"):
            try:
                output_file = open(options["output_file"], "w", encoding="utf-8")
                self.stdout = TeeWriter(original_stdout, output_file, options.get("quiet", False))
            except IOError as e:
                raise CommandError(f"Cannot open output file: {e}")

        try:
            action = options["action"]

            # Load action doesn't use the standard queryset builder
            if action == "load":
                self._action_load(options)
                return

            # Build queryset based on selection criteria
            queryset = self._build_queryset(options)

            if action == "list":
                self._action_list(queryset, options)
            elif action == "count":
                self._action_count(queryset, options)
            elif action == "inspect":
                self._action_inspect(queryset, options)
            elif action == "delete":
                self._action_delete(queryset, options)
            elif action == "audit":
                self._action_audit(queryset, options)
            elif action == "export":
                self._action_export(queryset, options)
        finally:
            if output_file:
                output_file.close()
                self.stdout = original_stdout

    def _build_queryset(self, options):
        """Build a queryset based on provided selection criteria."""
        qs = AreaOfInterest.objects.all()

        # Filter by single ID
        if options.get("aoi_id"):
            qs = qs.filter(id=options["aoi_id"])

        # Filter by multiple IDs
        if options.get("ids"):
            try:
                id_list = [int(x.strip()) for x in options["ids"].split(",")]
                qs = qs.filter(id__in=id_list)
            except ValueError:
                raise CommandError("--ids must be comma-separated integers (e.g., --ids=1,2,3)")

        # Filter by name (case-insensitive contains)
        if options.get("name"):
            qs = qs.filter(name__icontains=options["name"])

        # Apply Django ORM filters
        if options.get("filters"):
            for filter_expr in options["filters"]:
                try:
                    key, value = filter_expr.split("=", 1)
                    # Attempt numeric conversion
                    try:
                        value = float(value) if "." in value else int(value)
                    except ValueError:
                        pass  # Keep as string
                    qs = qs.filter(**{key: value})
                except ValueError:
                    raise CommandError(f"Invalid filter syntax: '{filter_expr}'. Use key=value format.")

        return qs

    def _action_list(self, queryset, options):
        """List AOIs with specified format."""
        count = queryset.count()

        if count == 0:
            self.stdout.write(self.style.WARNING("No AOIs found matching criteria."))
            return

        fmt = options["format"]

        if fmt == "simple":
            self.stdout.write(f"\nFound {count} AOI(s):\n")
            for aoi in queryset.order_by("id"):
                self.stdout.write(f"  [{aoi.id}] {aoi.name} ({aoi.sqkm:.2f} sq km)")

        elif fmt == "table":
            self.stdout.write(f"\n{'ID':<6} {'Name':<40} {'Sq Km':>12}")
            self.stdout.write("-" * 60)
            for aoi in queryset.order_by("id"):
                self.stdout.write(f"{aoi.id:<6} {aoi.name:<40} {aoi.sqkm:>12.2f}")
            self.stdout.write("-" * 60)
            self.stdout.write(f"Total: {count} record(s)\n")

        elif fmt == "csv":
            self.stdout.write("id,name,sqkm")
            for aoi in queryset.order_by("id"):
                # Escape commas in name
                name = f'"{aoi.name}"' if "," in aoi.name else aoi.name
                self.stdout.write(f"{aoi.id},{name},{aoi.sqkm}")

        elif fmt == "json":
            import json
            data = list(queryset.values("id", "name", "sqkm"))
            self.stdout.write(json.dumps(data, indent=2))

        elif fmt == "jira":
            self.stdout.write(f"\n||ID||Name||Sq Km||")
            for aoi in queryset.order_by("id"):
                self.stdout.write(f"|{aoi.id}|{aoi.name}|{aoi.sqkm:.2f}|")

    def _action_count(self, queryset, options):
        """Count AOIs matching criteria."""
        count = queryset.count()
        if options.get("quiet"):
            self.stdout.write(str(count))
        else:
            self.stdout.write(f"AOI count: {count}")

    def _action_inspect(self, queryset, options: dict) -> None:
        """
        Show detailed information for selected AOI(s) including imagery summary.
        
        Args:
            queryset: Filtered AOI queryset.
            options: Command options dict.
            
        Returns:
            None. Output written to stdout.
        """
        from django.db.models import Min, Max
        
        if not options.get("aoi_id") and queryset.count() > 5:
            raise CommandError("Inspect is limited to 5 records. Use --id or add filters.")

        for aoi in queryset:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"AOI ID:    {aoi.id}")
            self.stdout.write(f"Name:      {aoi.name}")
            self.stdout.write(f"Area:      {aoi.sqkm:.2f} sq km")
            self.stdout.write(f"Geometry:  {aoi.geometry.geom_type} with {aoi.geometry.num_coords} coordinates")
            self.stdout.write(f"Bounds:    {aoi.geometry.extent}")
            self.stdout.write(f"Centroid:  {aoi.geometry.centroid.coords}")
            
            # Show related records from ALL imagery sources
            ee_records = EarthExplorer.objects.filter(aoi_id=aoi)
            ee_count = ee_records.count()
            
            gegd_count = 0
            mgp_count = 0
            if HAS_GEGD:
                gegd_count = GEOINTDiscovery.objects.filter(aoi_id=aoi).count()
            if HAS_MGP:
                mgp_count = MaxarGeospatialPlatform.objects.filter(aoi_id=aoi).count()
            
            tasking_count = Tasking.objects.filter(aoi=aoi).count()
            
            total_imagery = ee_count + gegd_count + mgp_count
            
            self.stdout.write(f"\nRelated Records:")
            self.stdout.write(f"  EarthExplorer (USGS):    {ee_count}")
            if HAS_GEGD:
                self.stdout.write(f"  GEOINTDiscovery (GEGD):  {gegd_count}")
            else:
                self.stdout.write(f"  GEOINTDiscovery (GEGD):  (model not available)")
            if HAS_MGP:
                self.stdout.write(f"  MaxarGeospatial (MGP):   {mgp_count}")
            else:
                self.stdout.write(f"  MaxarGeospatial (MGP):   (model not available)")
            self.stdout.write(f"  --------------------------")
            self.stdout.write(f"  Total imagery:           {total_imagery}")
            self.stdout.write(f"  Taskings:                {tasking_count}")
            
            # Show imagery summary if records exist
            if ee_count > 0:
                self.stdout.write(f"\nEarthExplorer Imagery Summary:")
                
                # Date range
                date_agg = ee_records.aggregate(
                    min_date=Min('acquisition_date'),
                    max_date=Max('acquisition_date')
                )
                min_date = date_agg['min_date']
                max_date = date_agg['max_date']
                
                if min_date and max_date:
                    if min_date == max_date:
                        self.stdout.write(f"  Date:       {min_date}")
                    else:
                        self.stdout.write(f"  Date range: {min_date} to {max_date}")
                else:
                    self.stdout.write(f"  Date range: (no dates)")
                
                # Sensors (satellite field)
                sensors = list(
                    ee_records.values_list('satellite', flat=True)
                    .distinct()
                    .order_by('satellite')
                )
                if sensors:
                    self.stdout.write(f"  Sensors:    {', '.join(str(s) for s in sensors if s)}")
                
                # Vendors
                vendors = list(
                    ee_records.values_list('vendor', flat=True)
                    .distinct()
                    .order_by('vendor')
                )
                if vendors:
                    self.stdout.write(f"  Vendors:    {', '.join(str(v) for v in vendors if v)}")
            
            # GEGD summary if available
            if HAS_GEGD and gegd_count > 0:
                gegd_records = GEOINTDiscovery.objects.filter(aoi_id=aoi)
                self.stdout.write(f"\nGEOINTDiscovery Imagery Summary:")
                
                date_agg = gegd_records.aggregate(
                    min_date=Min('acquisition_date'),
                    max_date=Max('acquisition_date')
                )
                min_date = date_agg['min_date']
                max_date = date_agg['max_date']
                
                if min_date and max_date:
                    if min_date == max_date:
                        self.stdout.write(f"  Date:       {min_date}")
                    else:
                        self.stdout.write(f"  Date range: {min_date} to {max_date}")
                
                # Source field for GEGD
                sources = list(
                    gegd_records.values_list('source', flat=True)
                    .distinct()
                    .order_by('source')
                )
                if sources:
                    self.stdout.write(f"  Sources:    {', '.join(str(s) for s in sources if s)}")
            
            # MGP summary if available
            if HAS_MGP and mgp_count > 0:
                mgp_records = MaxarGeospatialPlatform.objects.filter(aoi_id=aoi)
                self.stdout.write(f"\nMaxarGeospatialPlatform Imagery Summary:")
                
                date_agg = mgp_records.aggregate(
                    min_date=Min('datetime'),
                    max_date=Max('datetime')
                )
                min_date = date_agg['min_date']
                max_date = date_agg['max_date']
                
                if min_date and max_date:
                    if min_date == max_date:
                        self.stdout.write(f"  Date:       {min_date}")
                    else:
                        self.stdout.write(f"  Date range: {min_date} to {max_date}")
                
                # Platform field for MGP
                platforms = list(
                    mgp_records.values_list('platform', flat=True)
                    .distinct()
                    .order_by('platform')
                )
                if platforms:
                    self.stdout.write(f"  Platforms:  {', '.join(str(p) for p in platforms if p)}")
            
            if total_imagery > 0 or tasking_count > 0:
                self.stdout.write(
                    self.style.WARNING(f"\n  WARNING: Deleting this AOI will orphan {total_imagery + tasking_count} record(s)!")
                )
                self.stdout.write("      (FKs use DO_NOTHING, so records persist but become orphaned)")

    def _action_load(self, options):
        """
        Load AOIs from GeoJSON files.
        
        Expected GeoJSON schema:
          - feature_id: AOI ID (integer, required for --preserve-ids)
          - name: Human-readable label
          - Shape_Area: Area in sq meters (optional, calculated if missing)
          - mask_applied: land/water/none per DL-016 (backward compat: landmask_applied)
        
        Filename convention: "{feature_id}.geojson" (e.g., "0135.geojson")
        
        CRS: Auto-reprojected to WGS84 if needed.
        """
        if not HAS_GEO_LIBS:
            raise CommandError(
                "Load action requires geopandas and pyproj.\n"
                "Install with: pip install geopandas pyproj --break-system-packages"
            )

        dry_run = options.get("dry_run")
        confirm = options.get("confirm")
        input_dir = options.get("input_dir")
        input_file = options.get("input_file")
        skip_duplicates = options.get("skip_duplicates")
        replace_duplicates = options.get("replace_duplicates")
        preserve_ids = options.get("preserve_ids")

        # Validate arguments
        if not dry_run and not confirm:
            raise CommandError(
                "Load requires --dry-run or --confirm.\n"
                "Example: python manage.py aoi load --input-dir=/path/to/geojsons --dry-run"
            )

        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")

        if not input_dir and not input_file:
            raise CommandError("Load requires --input-dir or --file.")

        if input_dir and input_file:
            raise CommandError("Cannot use both --input-dir and --file. Choose one.")

        if skip_duplicates and replace_duplicates:
            raise CommandError("Cannot use both --skip-duplicates and --replace-duplicates.")

        # Collect GeoJSON files
        from glob import glob
        from pathlib import Path

        if input_file:
            geojson_files = [input_file]
            if not Path(input_file).exists():
                raise CommandError(f"File not found: {input_file}")
        else:
            input_path = Path(input_dir)
            if not input_path.is_dir():
                raise CommandError(f"Directory not found: {input_dir}")
            geojson_files = glob(str(input_path / "*.geojson"))
            if not geojson_files:
                raise CommandError(f"No .geojson files found in {input_dir}")

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("Load AOIs from GeoJSON")
        self.stdout.write(f"{'='*60}\n")
        self.stdout.write(f"Source: {input_dir or input_file}")
        self.stdout.write(f"Files found: {len(geojson_files)}")
        self.stdout.write(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
        self.stdout.write(f"Preserve IDs: {preserve_ids}\n")

        # Process each file
        stats = {
            "processed": 0,
            "created": 0,
            "replaced": 0,
            "skipped_duplicate": 0,
            "skipped_empty": 0,
            "id_conflicts": 0,
            "errors": 0,
        }
        
        aois_to_create = []
        aois_to_replace = []
        errors = []

        for filepath in sorted(geojson_files):
            filename = Path(filepath).name
            try:
                aoi_data = self._parse_geojson_file(filepath)
                
                # Check for ID conflicts if preserving IDs
                if preserve_ids and aoi_data.get("aoi_id"):
                    existing_by_id = AreaOfInterest.objects.filter(id=aoi_data["aoi_id"]).first()
                    if existing_by_id:
                        if replace_duplicates:
                            aoi_data["replace_id"] = existing_by_id.id
                            aois_to_replace.append(aoi_data)
                            self.stdout.write(
                                f"  REPLACE (ID): [{aoi_data['aoi_id']}] {aoi_data['name']} "
                                f"({aoi_data['sqkm']:.2f} sq km)"
                            )
                            stats["processed"] += 1
                            continue
                        elif skip_duplicates:
                            stats["skipped_duplicate"] += 1
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  SKIP: {filename} - ID {aoi_data['aoi_id']} already exists"
                                )
                            )
                            continue
                        else:
                            # Check if it's the same name - might be intentional replacement
                            if existing_by_id.name != aoi_data["name"]:
                                errors.append(
                                    f"{filename}: ID {aoi_data['aoi_id']} already exists "
                                    f"as '{existing_by_id.name}'"
                                )
                                stats["id_conflicts"] += 1
                                continue
                
                # Check for existing AOI with same name
                existing = AreaOfInterest.objects.filter(name=aoi_data["name"]).first()
                
                if existing:
                    if skip_duplicates:
                        stats["skipped_duplicate"] += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"  SKIP: {aoi_data['name']} (already exists as ID {existing.id})"
                            )
                        )
                        continue
                    elif replace_duplicates:
                        aoi_data["replace_id"] = existing.id
                        aois_to_replace.append(aoi_data)
                        id_str = f"[{aoi_data['aoi_id']}] " if aoi_data.get("aoi_id") else ""
                        self.stdout.write(
                            f"  REPLACE: {id_str}{aoi_data['name']} (was ID {existing.id}, "
                            f"{aoi_data['sqkm']:.2f} sq km)"
                        )
                    else:
                        errors.append(
                            f"{filename}: AOI '{aoi_data['name']}' already exists (ID {existing.id})"
                        )
                        stats["errors"] += 1
                        continue
                else:
                    aois_to_create.append(aoi_data)
                    id_str = f"[{aoi_data['aoi_id']}] " if aoi_data.get("aoi_id") else "[auto] "
                    mask_val = aoi_data.get("landmask_applied", "N")
                    lm_str = f" [mask:{mask_val}]" if mask_val not in ("N", "none", "") else ""
                    self.stdout.write(
                        f"  CREATE: {id_str}{aoi_data['name']} ({aoi_data['sqkm']:.2f} sq km){lm_str}"
                    )

                stats["processed"] += 1

            except ValueError as e:
                if "empty" in str(e).lower():
                    stats["skipped_empty"] += 1
                    self.stdout.write(self.style.WARNING(f"  EMPTY: {filename}"))
                else:
                    errors.append(f"{filename}: {str(e)}")
                    stats["errors"] += 1
                    self.stdout.write(self.style.ERROR(f"  ERROR: {filename}: {e}"))
            except Exception as e:
                errors.append(f"{filename}: {str(e)}")
                stats["errors"] += 1
                self.stdout.write(self.style.ERROR(f"  ERROR: {filename}: {e}"))

        # Summary before action
        self.stdout.write(f"\n{'-'*60}")
        self.stdout.write("SUMMARY:")
        self.stdout.write(f"  Files processed:     {stats['processed']}")
        self.stdout.write(f"  To create:           {len(aois_to_create)}")
        self.stdout.write(f"  To replace:          {len(aois_to_replace)}")
        self.stdout.write(f"  Skipped (duplicate): {stats['skipped_duplicate']}")
        self.stdout.write(f"  Skipped (empty):     {stats['skipped_empty']}")
        self.stdout.write(f"  ID conflicts:        {stats['id_conflicts']}")
        self.stdout.write(f"  Errors:              {stats['errors']}")
        self.stdout.write(f"{'-'*60}\n")

        if errors:
            self.stdout.write(self.style.ERROR("ERRORS:"))
            for err in errors[:10]:
                self.stdout.write(f"  * {err}")
            if len(errors) > 10:
                self.stdout.write(f"  ... and {len(errors) - 10} more")
            self.stdout.write("")

        if not aois_to_create and not aois_to_replace:
            self.stdout.write(self.style.WARNING("No AOIs to load."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would create {len(aois_to_create)} and replace {len(aois_to_replace)} AOI(s). "
                    "No changes made."
                )
            )
            self.stdout.write("Run with --confirm to execute.")
            return

        # Execute
        if confirm:
            from django.contrib.gis.geos import GEOSGeometry

            with transaction.atomic():
                # Create new AOIs
                for aoi_data in aois_to_create:
                    geom = GEOSGeometry(aoi_data["geometry_wkt"])
                    
                    if preserve_ids and aoi_data.get("aoi_id"):
                        # Force specific ID - use raw SQL or direct assignment
                        aoi = AreaOfInterest(
                            id=aoi_data["aoi_id"],
                            name=aoi_data["name"],
                            geometry=geom,
                            sqkm=aoi_data["sqkm"]
                        )
                        aoi.save()
                    else:
                        AreaOfInterest.objects.create(
                            name=aoi_data["name"],
                            geometry=geom,
                            sqkm=aoi_data["sqkm"]
                        )
                    stats["created"] += 1

                # Replace existing AOIs
                for aoi_data in aois_to_replace:
                    geom = GEOSGeometry(aoi_data["geometry_wkt"])
                    AreaOfInterest.objects.filter(id=aoi_data["replace_id"]).update(
                        name=aoi_data["name"],
                        geometry=geom,
                        sqkm=aoi_data["sqkm"]
                    )
                    stats["replaced"] += 1

            # Generate summary
            result_lines = [
                f"",
                f"{'='*60}",
                f"EXECUTION COMPLETE",
                f"{'='*60}",
                f"Action: Load AOIs from GeoJSON",
                f"Timestamp: {datetime.now().isoformat()}",
                f"Source: {input_dir or input_file}",
                f"Preserve IDs: {preserve_ids}",
                f"AOIs created: {stats['created']}",
                f"AOIs replaced: {stats['replaced']}",
                f"Skipped (duplicate): {stats['skipped_duplicate']}",
                f"Skipped (empty): {stats['skipped_empty']}",
                f"ID conflicts: {stats['id_conflicts']}",
                f"Errors: {stats['errors']}",
                f"Total AOIs in DB: {AreaOfInterest.objects.count()}",
                f"{'='*60}",
            ]

            self.stdout.write(
                self.style.SUCCESS(
                    f"OK: Loaded {stats['created']} new, replaced {stats['replaced']} AOI(s)."
                )
            )

            for line in result_lines:
                self.stdout.write(line)

    def _parse_geojson_file(self, filepath):
        """
        Parse a GeoJSON file and extract AOI data.
        
        Returns dict with: name, geometry_wkt, sqkm, aoi_id (optional)
        
        Expected GeoJSON Field Definitions:
        -----------------------------------
        - OBJECTID:        System-managed, IGNORED (ArcGIS artifact)
        - Shape:           Geometry (automatic)
        - feature_id:      APPLICATION-LEVEL UNIQUE ID - This is the AOI ID
                           Persists across workflows, matches filename (e.g., 0135)
        - aoi_type:        "annotation" or "tasking" (future use)
        - name:            Human-readable label for display/dropdown
        - mask_applied: land/water/none per DL-016 (backward compat: landmask_applied)
        - Shape_Length:    System-calculated, informational
        - Shape_Area:      System-calculated area (sq meters in projected CRS)
        
        Filename convention: "{feature_id}.geojson" (e.g., "0135.geojson")
        
        CRS handling:
          - Detects CRS from GeoJSON (e.g., EPSG:3338)
          - Reprojects to WGS84 (EPSG:4326) for storage
        """
        from pathlib import Path
        import re
        
        gdf = gpd.read_file(filepath)
        
        if len(gdf) == 0:
            raise ValueError("GeoJSON contains no features (empty file)")
        
        if len(gdf) > 1:
            # If multiple features, dissolve into single geometry
            gdf = gdf.dissolve()
        
        row = gdf.iloc[0]
        geom = row.geometry
        
        # Parse filename for ID (fallback if feature_id not in properties)
        filename = Path(filepath).stem  # Remove .geojson
        parsed_id = None
        parsed_name = filename
        
        # Try to extract ID from filename
        # Patterns: "0135.geojson", "0135_SRKW_LM.geojson", "132-French Frigate.geojson"
        id_match = re.match(r'^(\d+)', filename)
        if id_match:
            parsed_id = int(id_match.group(1))
            # Extract name portion after ID if present
            name_match = re.match(r'^\d+[_-](.+?)(?:_(?:LM|noLM))?$', filename)
            if name_match:
                parsed_name = name_match.group(1).replace('_', ' ')
        
        # Extract properties
        props = {}
        for col in row.index:
            if col != 'geometry':
                props[col] = row[col]
        
        # Get AOI ID from feature_id (primary ID field)
        aoi_id = None
        for id_field in ['feature_id', 'FEATURE_ID', 'aoi_id', 'AOI_ID']:
            if id_field in props and props[id_field] is not None:
                try:
                    aoi_id = int(props[id_field])
                    break
                except (ValueError, TypeError):
                    pass
        
        # Filename-derived ID is fallback only
        if aoi_id is None and parsed_id is not None:
            aoi_id = parsed_id
        
        # Get name from properties
        name = None
        for name_field in ['name', 'Name', 'NAME']:
            if name_field in props and props[name_field]:
                name = str(props[name_field])
                break
        
        if not name:
            name = parsed_name
        
        # Handle CRS - reproject to WGS84 if necessary
        source_crs = gdf.crs
        if source_crs is not None and source_crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
            geom = gdf.iloc[0].geometry
        
        # Calculate area
        # Shape_Area from projected CRS (e.g., UTM) is in square meters
        # Shape_Area from WGS84 is in square degrees - NOT usable directly
        sqkm = None
        source_is_projected = source_crs is not None and not source_crs.is_geographic
        
        if source_is_projected:
            # Only trust Shape_Area if source was projected (meters)
            for area_field in ['Shape_Area', 'shape_area', 'SHAPE_AREA']:
                if area_field in props and props[area_field] is not None:
                    try:
                        val = float(props[area_field])
                        if val > 0:
                            sqkm = val / 1_000_000  # sq meters to sq km
                            break
                    except (ValueError, TypeError):
                        pass
        
        # Check for pre-calculated sqkm field
        if sqkm is None:
            for area_field in ['sqkm', 'SQKM', 'sq_km', 'SQ_KM']:
                if area_field in props and props[area_field] is not None:
                    try:
                        val = float(props[area_field])
                        if val > 0:
                            sqkm = val
                            break
                    except (ValueError, TypeError):
                        pass
        
        if sqkm is None or sqkm <= 0:
            # Calculate area by projecting to UTM
            if gdf.crs is None or gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs("EPSG:4326")
                geom = gdf.iloc[0].geometry
            
            centroid = geom.centroid
            utm_zone = int((centroid.x + 180) / 6) + 1
            hemisphere = "north" if centroid.y >= 0 else "south"
            utm_crs = CRS.from_proj4(
                f"+proj=utm +zone={utm_zone} +{hemisphere} +datum=WGS84"
            )
            
            gdf_proj = gdf.to_crs(utm_crs)
            area_m2 = gdf_proj.iloc[0].geometry.area
            sqkm = area_m2 / 1_000_000
        
        # Ensure final geometry is WGS84
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
            geom = gdf.iloc[0].geometry
        
        # Log additional metadata for debugging
        aoi_type = props.get('aoi_type') or props.get('AOI_TYPE') or 'unknown'
        landmask = props.get('mask_applied') or props.get('MASK_APPLIED') or props.get('landmask_applied') or props.get('LANDMASK_APPLIED') or 'N'
        
        result = {
            "name": str(name)[:50],  # Truncate to field max length
            "geometry_wkt": geom.wkt,
            "sqkm": float(sqkm),
            "aoi_type": aoi_type,
            "landmask_applied": landmask,
        }
        
        if aoi_id is not None:
            result["aoi_id"] = aoi_id
        
        return result

    def _action_audit(self, queryset, options):
        """
        Audit AOIs showing associated imagery record counts from ALL sources.
        
        Sources checked:
          - EarthExplorer (USGS) - always
          - GEOINTDiscovery (G-EGD) - if model available
          - MaxarGeospatialPlatform (MGP) - if model available
        
        Usage:
            python manage.py aoi audit                 # Full audit table
            python manage.py aoi audit --empty-only   # Only AOIs with no records
            python manage.py aoi audit --delete-empty --dry-run
            python manage.py aoi audit --delete-empty --confirm
        """
        empty_only = options.get("empty_only")
        delete_empty = options.get("delete_empty")
        dry_run = options.get("dry_run")
        confirm = options.get("confirm")
        
        # Safety check for delete
        if delete_empty and not dry_run and not confirm:
            raise CommandError(
                "--delete-empty requires --dry-run or --confirm.\n"
                "Example: python manage.py aoi audit --delete-empty --dry-run"
            )
        
        # Collect audit data
        audit_data = []
        empty_ids = []
        
        # Track totals by source
        total_ee = 0
        total_gegd = 0
        total_mgp = 0
        
        for aoi in queryset.order_by("id"):
            # Count imagery records from ALL sources
            ee_count = EarthExplorer.objects.filter(aoi_id=aoi).count()
            gegd_count = GEOINTDiscovery.objects.filter(aoi_id=aoi).count() if HAS_GEGD else 0
            mgp_count = MaxarGeospatialPlatform.objects.filter(aoi_id=aoi).count() if HAS_MGP else 0
            
            total_count = ee_count + gegd_count + mgp_count
            has_data = (total_count > 0)
            
            # Track totals
            total_ee += ee_count
            total_gegd += gegd_count
            total_mgp += mgp_count
            
            if not has_data:
                empty_ids.append(aoi.id)
            
            if empty_only and has_data:
                continue
                
            audit_data.append({
                "id": aoi.id,
                "name": aoi.name[:30],  # Shorter to fit more columns
                "sqkm": aoi.sqkm,
                "ee_count": ee_count,
                "gegd_count": gegd_count,
                "mgp_count": mgp_count,
                "total_count": total_count,
                "status": "HAS DATA" if has_data else "NO DATA"
            })
        
        # Output header
        self.stdout.write("")
        self.stdout.write("=" * 100)
        self.stdout.write("AOI AUDIT REPORT (All Imagery Sources)")
        self.stdout.write("=" * 100)
        self.stdout.write(f"Total AOIs scanned:      {queryset.count()}")
        self.stdout.write(f"AOIs with imagery:       {queryset.count() - len(empty_ids)}")
        self.stdout.write(f"AOIs with no imagery:    {len(empty_ids)}")
        self.stdout.write("-" * 100)
        self.stdout.write("IMAGERY RECORD TOTALS BY SOURCE:")
        self.stdout.write(f"  EarthExplorer (USGS):    {total_ee:>6}")
        if HAS_GEGD:
            self.stdout.write(f"  GEOINTDiscovery (GEGD):  {total_gegd:>6}")
        else:
            self.stdout.write(f"  GEOINTDiscovery (GEGD):  (model not available)")
        if HAS_MGP:
            self.stdout.write(f"  MaxarGeospatial (MGP):   {total_mgp:>6}")
        else:
            self.stdout.write(f"  MaxarGeospatial (MGP):   (model not available)")
        self.stdout.write(f"  --------------------------")
        self.stdout.write(f"  GRAND TOTAL:             {total_ee + total_gegd + total_mgp:>6}")
        self.stdout.write("-" * 100)
        
        # Output table header - adjust based on available models
        if HAS_GEGD and HAS_MGP:
            header = f"{'ID':>4}  {'Name':<30}  {'Sq Km':>10}  {'EE':>6}  {'GEGD':>6}  {'MGP':>6}  {'Total':>6}  {'Status':<8}"
        elif HAS_GEGD:
            header = f"{'ID':>4}  {'Name':<30}  {'Sq Km':>10}  {'EE':>6}  {'GEGD':>6}  {'Total':>6}  {'Status':<8}"
        elif HAS_MGP:
            header = f"{'ID':>4}  {'Name':<30}  {'Sq Km':>10}  {'EE':>6}  {'MGP':>6}  {'Total':>6}  {'Status':<8}"
        else:
            header = f"{'ID':>4}  {'Name':<30}  {'Sq Km':>10}  {'EE':>6}  {'Total':>6}  {'Status':<8}"
        
        self.stdout.write(header)
        self.stdout.write("-" * 100)
        
        for row in audit_data:
            if HAS_GEGD and HAS_MGP:
                line = f"{row['id']:>4}  {row['name']:<30}  {row['sqkm']:>10.2f}  {row['ee_count']:>6}  {row['gegd_count']:>6}  {row['mgp_count']:>6}  {row['total_count']:>6}  {row['status']:<8}"
            elif HAS_GEGD:
                line = f"{row['id']:>4}  {row['name']:<30}  {row['sqkm']:>10.2f}  {row['ee_count']:>6}  {row['gegd_count']:>6}  {row['total_count']:>6}  {row['status']:<8}"
            elif HAS_MGP:
                line = f"{row['id']:>4}  {row['name']:<30}  {row['sqkm']:>10.2f}  {row['ee_count']:>6}  {row['mgp_count']:>6}  {row['total_count']:>6}  {row['status']:<8}"
            else:
                line = f"{row['id']:>4}  {row['name']:<30}  {row['sqkm']:>10.2f}  {row['ee_count']:>6}  {row['total_count']:>6}  {row['status']:<8}"
            
            if row['status'] == 'NO DATA':
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)
        
        self.stdout.write("-" * 100)
        self.stdout.write(f"Total: {len(audit_data)} record(s) shown")
        self.stdout.write("")
        
        # Handle empty AOI deletion
        if delete_empty and empty_ids:
            empty_qs = AreaOfInterest.objects.filter(id__in=empty_ids)
            
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(f"[DRY RUN] Would delete {len(empty_ids)} AOI(s) with no imagery:")
                )
                for aoi in empty_qs:
                    self.stdout.write(f"  [{aoi.id}] {aoi.name}")
                self.stdout.write("")
                self.stdout.write("Run with --confirm to execute.")
            
            elif confirm:
                count_to_delete = len(empty_ids)
                deleted_count, details = empty_qs.delete()
                self.stdout.write(
                    self.style.SUCCESS(f"Deleted {count_to_delete} AOI(s) with no imagery.")
                )
                # Log if Django reports different count (indicates cascade deletes)
                if deleted_count != count_to_delete:
                    self.stdout.write(
                        f"  (Django reported {deleted_count} total objects deleted: {details})"
                    )
        
        elif delete_empty and not empty_ids:
            self.stdout.write(self.style.SUCCESS("No empty AOIs to delete."))

    def _action_delete(self, queryset, options):
        """Delete AOIs with safety checks."""
        dry_run = options.get("dry_run")
        confirm = options.get("confirm")
        select_all = options.get("all")
        nuclear = options.get("nuclear")

        # Safety: require explicit flags
        if not dry_run and not confirm:
            raise CommandError(
                "Delete requires --dry-run (to preview) or --confirm (to execute).\n"
                "Example: python manage.py aoi delete --all --dry-run"
            )

        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")

        # Safety: require --all for bulk deletes without other criteria
        has_criteria = any([
            options.get("aoi_id"),
            options.get("ids"),
            options.get("name"),
            options.get("filters")
        ])

        if not has_criteria and not select_all:
            raise CommandError(
                "Bulk delete requires --all flag or selection criteria.\n"
                "Examples:\n"
                "  python manage.py aoi delete --all --dry-run\n"
                "  python manage.py aoi delete --id=132 --confirm\n"
                "  python manage.py aoi delete --name='Gulf' --dry-run"
            )

        # Nuclear option: require extra confirmation for --all deletes
        if select_all and confirm and not nuclear:
            raise CommandError(
                "Deleting ALL AOIs requires the --i-really-want-to-delete-all flag.\n"
                "This prevents accidental purges of the entire AOI table.\n\n"
                "To proceed:\n"
                "  python manage.py aoi delete --all --confirm --i-really-want-to-delete-all"
            )

        count = queryset.count()

        if count == 0:
            self.stdout.write(self.style.WARNING("No AOIs match the criteria. Nothing to delete."))
            return

        # Show what will be deleted
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"AOIs selected for deletion: {count}")
        self.stdout.write(f"{'='*60}\n")

        for aoi in queryset.order_by("id")[:20]:  # Limit preview to 20
            self.stdout.write(f"  [{aoi.id}] {aoi.name}")

        if count > 20:
            self.stdout.write(f"  ... and {count - 20} more")

        self.stdout.write("")

        # Check for related records from ALL sources
        aoi_ids = list(queryset.values_list("id", flat=True))
        related_ee = EarthExplorer.objects.filter(aoi_id_id__in=aoi_ids).count()
        related_gegd = GEOINTDiscovery.objects.filter(aoi_id__in=aoi_ids).count() if HAS_GEGD else 0
        related_mgp = MaxarGeospatialPlatform.objects.filter(aoi_id__in=aoi_ids).count() if HAS_MGP else 0
        related_tasking = Tasking.objects.filter(aoi_id__in=aoi_ids).count()
        total_imagery = related_ee + related_gegd + related_mgp

        if total_imagery > 0 or related_tasking > 0:
            self.stdout.write(self.style.WARNING("WARNING: Related records exist (will be orphaned):"))
            if related_ee > 0:
                self.stdout.write(f"   EarthExplorer (USGS):   {related_ee}")
            if related_gegd > 0:
                self.stdout.write(f"   GEOINTDiscovery (GEGD): {related_gegd}")
            if related_mgp > 0:
                self.stdout.write(f"   MaxarGeospatial (MGP):  {related_mgp}")
            if total_imagery > 0:
                self.stdout.write(f"   --------------------------")
                self.stdout.write(f"   Total imagery records:  {total_imagery}")
            if related_tasking > 0:
                self.stdout.write(f"   Tasking records:        {related_tasking}")
            self.stdout.write("")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"[DRY RUN] Would delete {count} AOI(s). No changes made.")
            )
            if select_all:
                self.stdout.write(
                    "\nTo execute, run:\n"
                    "  python manage.py aoi delete --all --confirm --i-really-want-to-delete-all"
                )
            else:
                self.stdout.write("Run with --confirm to execute.")
            return

        # Execute deletion
        if confirm:
            from django.db import connection
            
            # Check if we're using SQLite (need to disable FK constraints)
            is_sqlite = connection.vendor == 'sqlite'
            deleted_count = 0
            details = {}
            
            try:
                if is_sqlite and (total_imagery > 0 or related_tasking > 0):
                    # IMPORTANT: PRAGMA foreign_keys must be set OUTSIDE a transaction
                    # and requires autocommit mode in Django
                    self.stdout.write("Temporarily disabling SQLite FK constraints...")
                    
                    # Ensure we're in autocommit mode for PRAGMA to work
                    old_autocommit = connection.get_autocommit()
                    if not old_autocommit:
                        connection.set_autocommit(True)
                    
                    with connection.cursor() as cursor:
                        cursor.execute("PRAGMA foreign_keys = OFF;")
                    
                    # Verify it worked
                    with connection.cursor() as cursor:
                        cursor.execute("PRAGMA foreign_keys;")
                        fk_status = cursor.fetchone()[0]
                        self.stdout.write(f"  FK constraints status: {'ON' if fk_status else 'OFF'}")
                    
                    # Now do the delete (without transaction.atomic since we need FK off)
                    deleted_count, details = queryset.delete()
                    
                    # Re-enable FK constraints
                    with connection.cursor() as cursor:
                        cursor.execute("PRAGMA foreign_keys = ON;")
                    
                    # Restore autocommit setting
                    if not old_autocommit:
                        connection.set_autocommit(False)
                    
                    self.stdout.write("Re-enabled SQLite FK constraints.")
                else:
                    # No FK conflicts, use normal transaction
                    with transaction.atomic():
                        deleted_count, details = queryset.delete()

            except Exception as e:
                # Make sure FK constraints are re-enabled even on error
                if is_sqlite:
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute("PRAGMA foreign_keys = ON;")
                    except Exception:
                        pass
                raise CommandError(f"Delete failed: {e}")

            # Generate summary
            result_lines = [
                f"",
                f"{'='*60}",
                f"EXECUTION COMPLETE",
                f"{'='*60}",
                f"Action: Delete AOIs",
                f"Timestamp: {datetime.now().isoformat()}",
                f"AOIs deleted: {deleted_count}",
                f"Orphaned imagery records:",
                f"  EarthExplorer (USGS):   {related_ee}",
                f"  GEOINTDiscovery (GEGD): {related_gegd}",
                f"  MaxarGeospatial (MGP):  {related_mgp}",
                f"  Total:                  {total_imagery}",
                f"Orphaned Tasking records: {related_tasking}",
                f"{'='*60}",
            ]

            self.stdout.write(
                self.style.SUCCESS(f"OK: Deleted {deleted_count} AOI(s).")
            )

            for line in result_lines:
                self.stdout.write(line)

            if details:
                self.stdout.write("\nDeletion details:")
                for model, cnt in details.items():
                    self.stdout.write(f"  {model}: {cnt}")

    def _action_export(self, queryset, options: dict) -> None:
        """
        Export selected AOIs to GeoJSON file.
        
        Args:
            queryset: Filtered AOI queryset.
            options: Command options dict.
            
        Returns:
            None. Writes GeoJSON to file.
        """
        import json
        
        export_file = options.get("export_file")
        
        if not export_file:
            raise CommandError(
                "Export requires --export-file.\n"
                "Example: python manage.py aoi export --ids=1,6,24 --export-file=aois.geojson"
            )
        
        count = queryset.count()
        
        if count == 0:
            self.stdout.write(self.style.WARNING("No AOIs match the criteria. Nothing to export."))
            return
        
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"Exporting {count} AOI(s) to GeoJSON")
        self.stdout.write(f"{'='*60}\n")
        
        # Build GeoJSON FeatureCollection
        features = []
        
        for idx, aoi in enumerate(queryset.order_by("id"), start=1):
            # Get geometry as GeoJSON
            geom_json = json.loads(aoi.geometry.geojson)
            
            # Calculate Shape_Area and Shape_Length from geometry (in WGS84 degrees)
            shape_area = aoi.geometry.area  # Square degrees
            shape_length = aoi.geometry.length  # Degrees
            
            # Build properties to match standard schema
            # Fields in standard: OBJECTID, feature_id, name, species_com, species_sci,
            #                     mask_applied, aoi_type, status, Shape_Length, Shape_Area
            # Fields in DB: id, name, sqkm, geometry
            # Missing from DB: species_com, species_sci, mask_applied, aoi_type, status
            feature_id_str = f"{aoi.id:04d}"
            
            properties = {
                "OBJECTID": idx,
                "feature_id": feature_id_str,
                "name": aoi.name,
                "Shape_Length": shape_length,
                "Shape_Area": shape_area,
            }
            
            feature = {
                "type": "Feature",
                "id": idx,
                "geometry": geom_json,
                "properties": properties
            }
            features.append(feature)
            
            self.stdout.write(f"  [{aoi.id}] {aoi.name} ({aoi.sqkm:.2f} sq km)")
        
        feature_collection = {
            "type": "FeatureCollection",
            "features": features
        }
        
        # Write to file (compact format to match standard)
        try:
            with open(export_file, 'w', encoding='utf-8') as f:
                json.dump(feature_collection, f, separators=(',', ':'))
            
            self.stdout.write(f"\n{'-'*60}")
            self.stdout.write(
                self.style.SUCCESS(f"[OK] Exported {count} AOI(s) to {export_file}")
            )
            
            # Summary block
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write("EXPORT COMPLETE")
            self.stdout.write(f"{'='*60}")
            self.stdout.write(f"Action: Export AOIs to GeoJSON")
            self.stdout.write(f"Timestamp: {datetime.now().isoformat()}")
            self.stdout.write(f"Records exported: {count}")
            self.stdout.write(f"Output file: {export_file}")
            self.stdout.write(f"{'='*60}")
            
        except IOError as e:
            raise CommandError(f"Failed to write export file: {e}")