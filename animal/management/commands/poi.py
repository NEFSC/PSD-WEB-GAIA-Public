"""
Management command for PointsOfInterest operations.

Supports listing, validating, inspecting, deleting, loading,
and managing POIs with full data lineage tracing.
See ``python manage.py poi --help`` for usage.
"""
# ------------------------------------------------------------------------------
# ----- poi.py -----------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Unified management command for PointsOfInterest operations.
#              Supports listing, validating, inspecting, deleting, loading,
#              and managing POIs with full data lineage tracing.
#
#    tickets:  GAIFAGP-447 (clean up orphaned POIs with NULL catalog_id)
#              GAIFAGP-466 (Data Governance epic)
#              GAIFAGP-451 (load GeoJSON POIs via DL-022 contract)
#              GAIFAGP-573 (consolidation + auto-provisioning)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - ETL table is authoritative for acquisition dates
#      - ETL.date -> POI.date_image_taken
#      - Join key: POI.catalog_id <-> ETL.id (both are VARCHAR/string identifiers,
#        NOT integer PKs). Type normalization applied to prevent silent mismatches.
#      - Data lineage: AOI -> EE/MGP/GEGD -> ETL -> POI
#      - POI -> Annotations (FK cascade on delete)
#
#    usage:    python manage.py poi --help
#              python manage.py poi describe
#              python manage.py poi describe --detail=count --null-dates
#              python manage.py poi list --null-dates --limit=50
#              python manage.py poi validate
#              python manage.py poi inspect --id=12345
#              python manage.py poi repair --dry-run
#              python manage.py poi delete --null-catalog-id --dry-run
#              python manage.py poi load --file X.geojson --project-name Y --dry-run
#              python manage.py poi generate --input-geotiff <URL> --output-geojson out.geojson
#
# ------------------------------------------------------------------------------

import argparse
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from animal.models import PointsOfInterest, ExtractTransformLoad
from animal.utils.model_helpers import get_optional_model
from animal.utils.utils import TeeWriter


class Command(BaseCommand):
    help = """
Manage PointsOfInterest records in the GAIA database.

SOURCE OF TRUTH:
  ETL table is authoritative for acquisition dates.
  Join key: POI.catalog_id <-> ETL.id (both string identifiers).
  Data lineage: AOI -> EE/MGP/GEGD -> ETL -> POI

ACTIONS:
  describe        Describe POI table (default: statistics with catalog breakdown)
                  --detail=count    Filtered count only
                  --detail=summary  Per-catalog-id rollup with vendor grouping
  list            List POIs with optional filtering
  validate        Check data integrity across full chain (AOI->EE->ETL->POI)
  inspect         Show detailed information for a specific POI with lineage
  repair          Repair orphan POIs with NULL catalog_id (GAIFAGP-447)
                  --dry-run   Diagnose (read-only report)
                  --confirm   Execute POI-to-POI matching repair
  delete          Delete POIs (requires --dry-run or --confirm)
  load            Load POIs from GeoJSON file (GAIFAGP-451, DL-022)
  generate        Run detection on GeoTIFF to produce GeoJSON (microsoft/whales)

EXAMPLES:
  # Describe POI table (default: statistics with catalog breakdown)
  python manage.py poi describe
  python manage.py poi describe --detail=count
  python manage.py poi describe --detail=count --null-dates
  python manage.py poi describe --detail=summary
  python manage.py poi describe --detail=summary --format=table
  python manage.py poi describe --detail=summary --format=csv --output=poi_summary.csv

  # List POIs with optional filtering
  python manage.py poi list --null-dates
  python manage.py poi list --catalog-id=1030010012345678
  python manage.py poi list --aoi=6

  # Validate POI data integrity (full chain)
  python manage.py poi validate

  # Inspect a specific POI with full lineage
  python manage.py poi inspect --id=12345

  # Repair orphan POIs with NULL catalog_id (GAIFAGP-447)
  python manage.py poi repair --dry-run
  python manage.py poi repair --dry-run --output=null_catalog_report.txt
  python manage.py poi repair --confirm --verbose

  # Delete POIs with NULL catalog_id (GAIFAGP-447)
  python manage.py poi delete --null-catalog-id --dry-run
  python manage.py poi delete --null-catalog-id --confirm

  # Delete specific POIs
  python manage.py poi delete --id=12345 --confirm
  python manage.py poi delete --ids=1,2,3 --dry-run
  python manage.py poi delete --filter="project_id=5" --dry-run

  # Delete all POIs (requires nuclear flag)
  python manage.py poi delete --all --confirm --i-really-want-to-delete-all

  # Preview POI load from GeoJSON (GAIFAGP-451)
  python manage.py poi load --file /path/to/detections.geojson --project-name narw_capecod_2020_2024 --dry-run

  # Execute POI load (by project ID)
  python manage.py poi load --file /path/to/detections.geojson --project 5 --confirm

  # Load with duplicate replacement
  python manage.py poi load --file /path/to/detections.geojson --project-name narw_capecod_2020_2024 --confirm --replace-duplicates

  # Generate interesting points from a COG, then load
  python manage.py poi generate --input-geotiff https://storage.blob.core.windows.net/data/cogs/image.tif --output-geojson /tmp/detections.geojson
  python manage.py poi generate --input-geotiff /path/to/local.tif --output-geojson out.geojson --method big_window --difference-threshold 20
  python manage.py poi generate --input-geotiff <URL> --output-geojson out.geojson --auto-threshold --land-mask /path/to/mask.shp

LINKAGE (ETL -> POI):
  Join key: POI.catalog_id <-> ETL.id (both are string catalog IDs).
  ETL.date -> POI.date_image_taken

DATA LINEAGE:
  Full chain validation checks:
    AOI -> EarthExplorer (aoi_id FK)
    AOI -> GEOINTDiscovery (aoi_id FK) [if available]
    AOI -> MaxarGeospatialPlatform (aoi_id FK) [if available]
    EE/GEGD/MGP -> ETL (trigger-populated)
    ETL -> POI (catalog_id join)
"""

    def create_parser(self, prog_name, subcommand, **kwargs):
        """Use RawDescriptionHelpFormatter to preserve example formatting.

        Args:
            prog_name: Program name for help text.
            subcommand: Subcommand name.
            **kwargs: Passed to super().create_parser().

        Returns:
            Configured ArgumentParser.
        """
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser):
        """Register CLI arguments and subactions.

        Args:
            parser: ArgumentParser instance from Django.
        """
        # Positional: action
        parser.add_argument(
            "action",
            choices=["describe", "list", "validate", "inspect",
                     "repair", "delete", "load", "generate"],
            help="Action to perform"
        )

        # Selection/filtering
        selection = parser.add_argument_group("Selection Criteria")
        selection.add_argument(
            "--id",
            type=int,
            dest="poi_id",
            help="Select specific POI by ID (required for inspect)"
        )
        selection.add_argument(
            "--ids",
            type=str,
            help="Comma-separated list of POI IDs (for delete)"
        )
        selection.add_argument(
            "--null-dates",
            action="store_true",
            help="Select only records where date_image_taken is NULL"
        )
        selection.add_argument(
            "--has-dates",
            action="store_true",
            help="Select only records where date_image_taken is populated"
        )
        selection.add_argument(
            "--null-catalog-id",
            action="store_true",
            help="Select only records where catalog_id is NULL (orphaned POIs)"
        )
        selection.add_argument(
            "--catalog-id",
            type=str,
            help="Filter by catalog_id"
        )
        selection.add_argument(
            "--vendor-id",
            type=str,
            help="Filter by vendor_id"
        )
        selection.add_argument(
            "--entity-id",
            type=str,
            help="Filter by entity_id"
        )
        selection.add_argument(
            "--project",
            type=int,
            help="Filter by project ID"
        )
        selection.add_argument(
            "--aoi",
            type=int,
            help="Filter by AOI ID (traces through ETL->source catalog->AOI chain)"
        )
        selection.add_argument(
            "--filter",
            type=str,
            action="append",
            dest="filters",
            help="Django ORM filter (e.g., --filter='area__gt=100'). Can be repeated."
        )
        selection.add_argument(
            "--all",
            action="store_true",
            help="Select all records (required for bulk delete without other criteria)"
        )
        selection.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Limit number of records shown in list (default: 100)"
        )

        # Safety flags
        safety = parser.add_argument_group("Safety Flags (required for modifications)")
        safety.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without modifying the database"
        )
        safety.add_argument(
            "--confirm",
            action="store_true",
            help="Actually execute modifications"
        )
        safety.add_argument(
            "--i-really-want-to-delete-all",
            action="store_true",
            dest="nuclear",
            help="Required safety flag for --all deletes (prevents accidental purges)"
        )

        # Processing options
        processing = parser.add_argument_group("Processing Options")
        processing.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Records per batch for bulk operations (default: 1000)"
        )
        processing.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed per-record information"
        )

        # Output
        output = parser.add_argument_group("Output Options")
        output.add_argument(
            "--detail",
            choices=["stats", "count", "summary"],
            default=None,
            help="Detail level for describe action (default: stats)"
        )
        output.add_argument(
            "--format",
            choices=["simple", "table", "csv", "jira"],
            default="simple",
            help="Output format for list/describe summary (jira = pasteable summary)"
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
            help=(
                "Write output to file (in addition"
                " to console). Use with --quiet"
                " for file-only."
            )
        )

        # Load action options (GAIFAGP-451)
        load_opts = parser.add_argument_group("Load Options (poi load)")
        load_opts.add_argument(
            "--file",
            type=str,
            dest="input_file",
            help="Path to GeoJSON file (required for load action)"
        )
        load_opts.add_argument(
            "--project-name",
            type=str,
            dest="project_name",
            help="Project value or label (e.g., narw_capecod_2020_2024). "
                 "Alternative to --project (ID) for the load action."
        )
        load_opts.add_argument(
            "--skip-duplicates",
            action="store_true",
            default=True,
            help="Skip features whose sample_idx already exists in project (default)"
        )
        load_opts.add_argument(
            "--replace-duplicates",
            action="store_true",
            help="Replace existing records on sample_idx collision within project"
        )

        # Generate action options (GAIFAGP-573, restores GAIFAGP-452 capability)
        gen_opts = parser.add_argument_group("Generate Options (poi generate)")
        gen_opts.add_argument(
            "--input-geotiff",
            type=str,
            dest="input_geotiff",
            help="URL or path to Cloud Optimized GeoTIFF (required for generate)"
        )
        gen_opts.add_argument(
            "--output-geojson",
            type=str,
            dest="output_geojson",
            help="Output GeoJSON file path (required for generate)"
        )
        gen_opts.add_argument(
            "--method",
            type=str,
            default="big_window",
            dest="detect_method",
            help="Detection method: big_window (default), rolling_window, gmm"
        )
        gen_opts.add_argument(
            "--difference-threshold",
            type=float,
            dest="difference_threshold",
            help="Threshold in standard deviations (default: script default)"
        )
        gen_opts.add_argument(
            "--auto-threshold",
            action="store_true",
            dest="auto_difference_threshold",
            help="Auto-calculate difference threshold from data distribution"
        )
        gen_opts.add_argument(
            "--area-threshold",
            type=float,
            dest="area_threshold",
            help="Minimum feature size in map units (default: script default)"
        )
        gen_opts.add_argument(
            "--window-size",
            type=int,
            dest="big_window_size",
            help="Window size for big_window method (default: script default)"
        )
        gen_opts.add_argument(
            "--land-mask",
            type=str,
            dest="land_mask_fn",
            help="Path to land mask vector file"
        )
        gen_opts.add_argument(
            "--study-area",
            type=str,
            dest="study_area_fn",
            help="Path to study area vector file"
        )
        gen_opts.add_argument(
            "--bands",
            type=str,
            help="Comma-separated 1-based band indices (e.g., '1,2,3')"
        )
        gen_opts.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing output GeoJSON file"
        )

    def handle(self, *args, **options):
        """Dispatch to action method based on CLI args.

        Args:
            *args: Positional args (unused).
            **options: Parsed CLI options dict.
        """
        # Set up output redirection if --output specified
        output_file = None
        original_stdout = self.stdout
        
        if options.get("output_file"):
            try:
                output_file = open(options["output_file"], "w", encoding="utf-8")
                self.stdout = TeeWriter(
                    original_stdout, output_file, options.get("quiet", False))
            except IOError as e:
                raise CommandError(f"Cannot open output file: {e}")

        try:
            action = options["action"]

            if action == "describe":
                self._action_describe(options)
            elif action == "list":
                self._action_list(options)
            elif action == "validate":
                self._action_validate(options)
            elif action == "inspect":
                self._action_inspect(options)
            elif action == "repair":
                self._action_repair(options)
            elif action == "delete":
                self._action_delete(options)
            elif action == "load":
                self._action_load(options)
            elif action == "generate":
                self._action_generate(options)
        finally:
            if output_file:
                output_file.close()
                self.stdout = original_stdout

    def _get_catalog_ids_for_aoi(self, aoi_id):
        """
        Get all catalog_ids linked to an AOI through EE/MGP/GEGD -> ETL chain.
        
        Args:
            aoi_id: AreaOfInterest primary key.

        Returns:
            Set of catalog_id strings.
        """
        catalog_ids = set()
        
        # EarthExplorer -> ETL (via vendor_id or entity_id)
        EarthExplorer, has_ee = get_optional_model('EarthExplorer')
        if has_ee:
            ee_records = EarthExplorer.objects.filter(aoi_id=aoi_id).values_list(
                "vendor_id", "entity_id"
            )
            for vendor_id, entity_id in ee_records:
                if vendor_id:
                    etl_ids = ExtractTransformLoad.objects.filter(
                        vendor_id=str(vendor_id)
                    ).values_list("id", flat=True)
                    catalog_ids.update(str(eid) for eid in etl_ids)
                if entity_id:
                    etl_ids = ExtractTransformLoad.objects.filter(
                        entity_id=str(entity_id)
                    ).values_list("id", flat=True)
                    catalog_ids.update(str(eid) for eid in etl_ids)
        
        # GEOINTDiscovery -> ETL
        # NOTE: GEGD.legacy_id maps to ETL.id (not ETL.vendor_id)
        GEOINTDiscovery, has_gegd = get_optional_model('GEOINTDiscovery')
        if has_gegd:
            gegd_records = GEOINTDiscovery.objects.filter(aoi_id=aoi_id).values_list(
                "legacy_id", flat=True
            )
            for legacy_id in gegd_records:
                if legacy_id:
                    # GEGD.legacy_id IS the ETL.id directly
                    catalog_ids.add(str(legacy_id))
        
        # MaxarGeospatialPlatform -> ETL
        # NOTE: MGP.id maps to ETL.id (not ETL.vendor_id)
        MaxarGeospatialPlatform, has_mgp = (
            get_optional_model('MaxarGeospatialPlatform')
        )
        if has_mgp:
            mgp_records = (
                MaxarGeospatialPlatform.objects
                .filter(aoi_id=aoi_id)
                .values_list("id", flat=True)
            )
            for mgp_id in mgp_records:
                if mgp_id:
                    # MGP.id IS the ETL.id directly
                    catalog_ids.add(str(mgp_id))
        
        return catalog_ids

    def _build_queryset(self, options):
        """Build POI queryset based on selection criteria.

        Args:
            options: Parsed CLI options dict.

        Returns:
            Filtered PointsOfInterest QuerySet.
        """
        qs = PointsOfInterest.objects.all()

        if options.get("poi_id"):
            qs = qs.filter(id=options["poi_id"])

        if options.get("null_dates"):
            qs = qs.filter(date_image_taken__isnull=True)

        if options.get("has_dates"):
            qs = qs.filter(date_image_taken__isnull=False)

        if options.get("catalog_id"):
            qs = qs.filter(catalog_id=options["catalog_id"])

        if options.get("vendor_id"):
            qs = qs.filter(vendor_id=options["vendor_id"])

        if options.get("entity_id"):
            qs = qs.filter(entity_id=options["entity_id"])

        if options.get("project"):
            qs = qs.filter(project_id=options["project"])

        # AOI filter - traces through ETL -> source catalog -> AOI
        if options.get("aoi"):
            catalog_ids = self._get_catalog_ids_for_aoi(options["aoi"])
            if catalog_ids:
                qs = qs.filter(catalog_id__in=catalog_ids)
            else:
                # No catalog_ids found for this AOI - return empty queryset
                qs = qs.none()

        if options.get("null_catalog_id"):
            qs = qs.filter(catalog_id__isnull=True)

        if options.get("filters"):
            for filter_expr in options["filters"]:
                try:
                    key, value = filter_expr.split("=", 1)
                    try:
                        value = float(value) if "." in value else int(value)
                    except ValueError:
                        pass
                    qs = qs.filter(**{key: value})
                except ValueError:
                    raise CommandError(f"Invalid filter: '{filter_expr}'")

        return qs

    def _action_load(self, options):
        """Load POIs from a GeoJSON file. Thin wrapper per §3.8.

        Args:
            options: Parsed CLI options dict.
        """
        from animal.utils.poi_loader import load_pois

        dry_run, confirm = options.get("dry_run"), options.get("confirm")
        input_file = options.get("input_file")
        project_id = options.get("project")
        project_name = options.get("project_name")
        replace_dupes = options.get("replace_duplicates", False)

        if not dry_run and not confirm:
            raise CommandError("Load requires --dry-run or --confirm.")
        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")
        if not input_file:
            raise CommandError("Load requires --file <path>.")
        if not project_id and not project_name:
            raise CommandError("Load requires --project <ID> or --project-name <value>.")
        if project_id and project_name:
            raise CommandError("Use --project or --project-name, not both.")

        project_identifier = str(project_id) if project_id else project_name
        w = self.stdout.write
        mode = "DRY RUN" if dry_run else "EXECUTE"
        w(f"\n{'=' * 60}\nLoad POIs from GeoJSON\n{'=' * 60}")
        w(f"File: {input_file}  |  Project: {project_identifier}  |  Mode: {mode}")

        try:
            result = load_pois(
                filepath=input_file, project_identifier=project_identifier,
                dry_run=dry_run, replace_duplicates=replace_dupes,
                batch_size=options.get("batch_size", 1000),
            )
        except (FileNotFoundError, ValueError) as e:
            raise CommandError(str(e))

        r = result
        w(f"Project resolved: {r['project_label']}\n{'-' * 60}")
        w(f"  Features: {r['total_features']}  |  Load: {r['loaded']}"
          f"  |  Dupes: {r['duplicates']}  |  Replace: {r['replaced']}"
          f"  |  Skip: {r['skipped']}  |  Errors: {len(r['errors'])}")
        for warn in r["etl_warnings"]:
            w(self.style.WARNING(f"  ETL: {warn}"))
        for err in r["errors"][:10]:
            w(self.style.ERROR(f"  ERR: {err}"))
        if len(r["errors"]) > 10:
            w(f"  ... and {len(r['errors']) - 10} more errors")

        if dry_run:
            w(self.style.WARNING(
                f"[DRY RUN] Would load {r['loaded']}, replace {r['replaced']}. "
                "No changes made. Run with --confirm to execute."))
        else:
            w(self.style.SUCCESS(
                f"OK: Loaded {r['loaded']} new, replaced {r['replaced']} POI(s)."))
            w(f"Total POIs in DB: {PointsOfInterest.objects.count()}")

    def _action_generate(self, options):
        """
        Generate interesting points from a GeoTIFF using Microsoft's
        detection tool. Thin wrapper — delegates to poi_generate.

        Args:
            options: Parsed CLI options dict.
        """
        from animal.utils.poi_generate import generate_interesting_points

        input_geotiff = options.get("input_geotiff")
        output_geojson = options.get("output_geojson")

        if not input_geotiff:
            raise CommandError(
                "generate requires --input-geotiff <URL or path>.")
        if not output_geojson:
            raise CommandError(
                "generate requires --output-geojson <path>.")

        w = self.stdout.write
        w(f"\n{'='*60}")
        w("Generate Interesting Points (Microsoft AI for Good)")
        w(f"{'='*60}")
        w(f"Input:  {input_geotiff}")
        w(f"Output: {output_geojson}")
        w(f"Method: {options.get('detect_method', 'big_window')}")

        result = generate_interesting_points(
            input_fn=input_geotiff,
            output_fn=output_geojson,
            method=options.get("detect_method", "big_window"),
            difference_threshold=options.get("difference_threshold"),
            auto_difference_threshold=options.get(
                "auto_difference_threshold", False),
            area_threshold=options.get("area_threshold"),
            big_window_size=options.get("big_window_size"),
            land_mask_fn=options.get("land_mask_fn"),
            study_area_fn=options.get("study_area_fn"),
            bands=options.get("bands"),
            overwrite=options.get("overwrite", False),
        )

        if result["success"]:
            w(self.style.SUCCESS(
                f"\nGeneration complete. Output: {result['output_fn']}"))
            w(f"\nTo load into database:")
            w(f"  python manage.py poi load "
              f"--file {result['output_fn']} "
              f"--project-name <project> --dry-run")
        else:
            w(self.style.ERROR(f"\nGeneration failed: {result['error']}"))
            if result["stderr"]:
                w(f"stderr: {result['stderr'][:500]}")
            raise CommandError(
                f"generate_interesting_points failed: {result['error']}")

    def _action_describe(self, options):
        """
        Describe POI table. Default shows statistics with catalog breakdown.
        --detail=count shows filtered count only. --detail=summary shows
        per-catalog-id rollup with vendor grouping and format options.

        Args:
            options: Parsed CLI options dict.
        """
        detail = options.get("detail") or "stats"

        if detail == "count":
            return self._describe_count(options)
        elif detail == "summary":
            return self._describe_summary(options)
        else:
            return self._describe_stats(options)

    def _describe_stats(self, options):
        """Statistics with catalog-based breakdown (default describe)."""
        qs = self._build_queryset(options)
        total = qs.count()
        with_dates = (
            qs.filter(date_image_taken__isnull=False).count()
        )
        null_dates = (
            qs.filter(date_image_taken__isnull=True).count()
        )

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("PointsOfInterest Table Statistics")
        self.stdout.write(f"{'='*60}\n")

        self.stdout.write(f"Total records:           {total:>10}")
        msg = (
            f"With date_image_taken:   {with_dates:>10} "
            f"({100*with_dates/total if total else 0:.1f}%)"
        )
        self.stdout.write(msg)
        msg = (
            f"NULL date_image_taken:   {null_dates:>10} "
            f"({100*null_dates/total if total else 0:.1f}%)"
        )
        self.stdout.write(msg)

        # Group by project
        by_project = (
            qs
            .values("project__label")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        self.stdout.write(f"\nBy Project:")
        self.stdout.write(f"  {'Project':<35} {'Count':>10}")
        self.stdout.write(f"  {'-'*35} {'-'*10}")
        for row in by_project:
            label = row["project__label"] or "(No Project)"
            self.stdout.write(f"  {label:<35} {row['count']:>10}")

        # Catalog-based summary
        self.stdout.write(f"\n{'-'*60}")
        self.stdout.write("POI Summary by Catalog ID")
        self.stdout.write(f"{'-'*60}\n")

        # Get all POIs grouped by catalog_id with their dates
        from collections import defaultdict
        
        catalog_data = defaultdict(lambda: {
            'dates': set(),
            'count': 0
        })
        
        # Fetch relevant fields
        poi_records = qs.values(
            'catalog_id', 'date_image_taken'
        )
        
        for poi in poi_records:
            cat_id = poi['catalog_id']
            if cat_id:
                catalog_data[cat_id]['count'] += 1
                if poi['date_image_taken']:
                    catalog_data[cat_id]['dates'].add(poi['date_image_taken'])
        
        # Count warnings
        date_mismatch_count = 0
        null_catalog_count = (
            qs.filter(catalog_id__isnull=True).count()
        )
        
        # Sort by count descending
        sorted_catalogs = sorted(
            catalog_data.items(),
            key=lambda x: x[1]['count'],
            reverse=True
        )
        
        # Header
        self.stdout.write(f"{'Catalog ID':<22} {'POIs':>8} {'Date':>12} {'Status':<8}")
        self.stdout.write(f"{'-'*22} {'-'*8} {'-'*12} {'-'*8}")
        
        # Show top 20 (or all if fewer)
        display_limit = min(20, len(sorted_catalogs))
        
        for catalog_id, data in sorted_catalogs[:display_limit]:
            # Check date consistency
            if len(data['dates']) == 0:
                date_str = 'NULL'
                status = '[!]'
                date_mismatch_count += 1
            elif len(data['dates']) == 1:
                date_str = str(list(data['dates'])[0])
                status = '[OK]'
            else:
                date_str = 'MULTIPLE'
                status = '[!]'
                date_mismatch_count += 1
            
            # Truncate catalog_id for display
            cat_display = str(catalog_id)[:22]
            
            self.stdout.write(
                f"{cat_display:<22} {data['count']:>8} {date_str:>12} {status:<8}"
            )
        
        if len(sorted_catalogs) > display_limit:
            self.stdout.write(
                f"\n... and {len(sorted_catalogs) - display_limit} more catalog IDs")
        
        # Summary
        self.stdout.write(f"\n{'-'*60}")
        self.stdout.write(f"Total unique catalog IDs: {len(catalog_data)}")
        
        if null_catalog_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"[!] {null_catalog_count} POIs have NULL catalog_id")
            )
        
        if date_mismatch_count > 0:
            msg = (
                f"[!] {date_mismatch_count} catalog"
                f" IDs have date issues"
                f" (NULL or multiple dates)"
            )
            self.stdout.write(
                self.style.WARNING(msg)
            )
        
        if null_dates > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"\n[!] {null_dates} records have NULL date_image_taken")
            )

    def _action_list(self, options):
        """
        List POIs with optional filtering.

        Args:
            options: Parsed CLI options dict.
        """
        qs = self._build_queryset(options)
        limit = options["limit"]
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.WARNING("No POIs match criteria."))
            return

        self.stdout.write(f"\nShowing {min(limit, total)} of {total} POI(s):\n")

        fmt = options["format"]

        if fmt == "table":
            self.stdout.write(
                f"{'ID':<8} {'Catalog ID':<18} {'Date Taken':<12} {'Project':<20}"
            )
            self.stdout.write("-" * 60)

        for poi in qs.select_related("project").order_by("id")[:limit]:
            date_str = str(poi.date_image_taken) if poi.date_image_taken else "NULL"
            project_str = poi.project.label if poi.project else "None"

            if fmt == "simple":
                self.stdout.write(
                    f"  [{poi.id}] {poi.catalog_id or 'N/A':<16} | "
                    f"date: {date_str:<10} | project: {project_str}"
                )
            elif fmt == "table":
                self.stdout.write(
                    (
                        f"{poi.id:<8} {(poi.catalog_id or 'N/A')[:18]:<18} "
                        f"{date_str:<12} {project_str[:20]:<20}"
                    )
                )
            elif fmt == "csv":
                self.stdout.write(f"{poi.id},{poi.catalog_id},{date_str},{project_str}")

        if total > limit:
            self.stdout.write(
                f"\n... {total - limit} more records. Use --limit to see more.")

    def _describe_count(self, options):
        """Count POIs matching criteria (describe --detail=count)."""
        qs = self._build_queryset(options)
        count = qs.count()

        if options.get("quiet"):
            self.stdout.write(str(count))
        else:
            self.stdout.write(f"POI count: {count}")

    def _describe_summary(self, options):
        """Per-catalog-id rollup with vendor grouping (describe --detail=summary)."""
        qs = self._build_queryset(options)
        limit = options.get("limit", 100)
        fmt = options.get("format", "simple")
        
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("POI Summary by Catalog ID")
        self.stdout.write(f"{'='*60}\n")
        
        # Get unique catalog_ids
        catalog_ids = list(
            qs.filter(catalog_id__isnull=False)
            .values_list("catalog_id", flat=True)
            .distinct()
            .order_by("catalog_id")
        )
        
        total_catalogs = len(catalog_ids)
        self.stdout.write(f"Total unique catalog_ids: {total_catalogs}")
        
        if total_catalogs == 0:
            self.stdout.write(self.style
                .WARNING("No catalog_ids found matching criteria."))
            return
        
        # Collect summary data
        summary_rows = []
        date_warnings = []
        
        for catalog_id in catalog_ids[:limit]:
            pois_for_catalog = qs.filter(catalog_id=catalog_id)
            
            # Get POI count
            poi_count = pois_for_catalog.count()
            
            # Get unique vendor_ids
            vendor_ids = list(
                pois_for_catalog
                .filter(vendor_id__isnull=False)
                .values_list("vendor_id", flat=True)
                .distinct()
            )
            vendor_ids_str = (
                ", ".join(str(v) for v in vendor_ids) if vendor_ids else "(none)"
            )
            
            # Get unique dates
            dates = list(
                pois_for_catalog
                .values_list("date_image_taken", flat=True)
                .distinct()
            )
            
            # Check for date consistency
            if len(dates) > 1:
                dates_str = ", ".join(str(d) for d in dates if d)
                date_warnings.append(
                    f"catalog_id={catalog_id}: multiple dates [{dates_str}]")
                date_display = f"[!] {dates_str}"
            elif len(dates) == 1:
                date_display = str(dates[0]) if dates[0] else "NULL"
            else:
                date_display = "NULL"
            
            summary_rows.append({
                "catalog_id": catalog_id,
                "vendor_ids": vendor_ids_str,
                "poi_count": poi_count,
                "date": date_display,
            })
        
        # Output based on format
        if fmt == "csv":
            self.stdout.write("catalog_id,vendor_ids,poi_count,date_image_taken")
            for row in summary_rows:
                # Escape commas in vendor_ids
                vendor_escaped = f'"{row["vendor_ids"]}"' if "," in row[
                    "vendor_ids"] else row["vendor_ids"]
                csv_row = (
                    f'{row["catalog_id"]},'
                    f'{vendor_escaped},'
                    f'{row["poi_count"]},'
                    f'{row["date"]}'
                )
                self.stdout.write(csv_row)
        
        elif fmt == "table":
            self.stdout.write(
                f"\n{'Catalog ID':<22} {'Vendor ID(s)':<30} {'POIs':>8} {'Date':>12}")
            self.stdout.write(f"{'-'*22} {'-'*30} {'-'*8} {'-'*12}")
            for row in summary_rows:
                vendor_truncated = row["vendor_ids"][
                    :28] + ".." if len(row["vendor_ids"]) > 30 else row["vendor_ids"]
                self.stdout.write(
                    (
                        f'{row["catalog_id"]:<22} {vendor_truncated:<30} '
                        f'{row["poi_count"]:>8} {row["date"]:>12}'
                    )
                )
        
        else:  # simple format
            self.stdout.write(
                f"\n{'Catalog ID':<22} {'POIs':>8}  {'Date':<12}  Vendor ID(s)")
            self.stdout.write(f"{'-'*22} {'-'*8}  {'-'*12}  {'-'*30}")
            for row in summary_rows:
                self.stdout.write(
                    (
                        f'{row["catalog_id"]:<22} {row["poi_count"]:>8} '
                        f' {row["date"]:<12}  {row["vendor_ids"]}'
                    )
                )
        
        # Show truncation notice
        if total_catalogs > limit:
            msg = (
                f"\n... showing {limit} of {total_catalogs} catalog_ids. Use --limit "
                f"to see more."
            )
            self.stdout.write(msg)
        
        # Report date warnings
        if date_warnings:
            self.stdout.write(f"\n{'-'*60}")
            self.stdout.write(self.style
                .WARNING(f"[!] DATE CONSISTENCY WARNINGS ({len(date_warnings)}):"))
            for warning in date_warnings[:20]:
                self.stdout.write(self.style.WARNING(f"  {warning}"))
            if len(date_warnings) > 20:
                self.stdout.write(self.style
                    .WARNING(f"  ... and {len(date_warnings) - 20} more"))
        else:
            self.stdout.write(f"\n{'-'*60}")
            self.stdout.write(self.style
                .SUCCESS("[OK] All catalog_ids have consistent dates."))
        
        # Summary stats
        self.stdout.write(f"\n{'='*60}")
        total_pois = sum(row["poi_count"] for row in summary_rows)
        self.stdout.write(f"Catalog IDs shown:       {len(summary_rows)}")
        self.stdout.write(f"Total POIs (shown):      {total_pois}")
        if date_warnings:
            self.stdout.write(self.style
                .WARNING(f"Date warnings:           {len(date_warnings)}"))

    def _action_inspect(self, options):
        """
        Show detailed information for a specific POI with full data lineage.
        Thin wrapper — delegates to inspect_poi().

        Args:
            options: Parsed CLI options dict.
        """
        from animal.utils.poi_inspection import inspect_poi

        poi_id = options.get("poi_id")
        if not poi_id:
            raise CommandError(
                "inspect requires --id=<poi_id>.\n"
                "Example: python manage.py poi inspect --id=12345"
            )

        try:
            r = inspect_poi(poi_id)
        except PointsOfInterest.DoesNotExist:
            raise CommandError(f"POI with id={poi_id} not found.")

        w = self.stdout.write

        w(f"\n{'='*60}")
        w("POI INSPECTION REPORT")
        w(f"{'='*60}\n")

        # POI record
        p = r["poi"]
        w("POI RECORD:")
        w(f"  ID:                    {p['id']}")
        w(f"  catalog_id:            {p['catalog_id'] or 'NULL'}")
        w(f"  vendor_id:             {p['vendor_id'] or 'NULL'}")
        w(f"  entity_id:             {p['entity_id'] or 'NULL'}")
        w(f"  date_image_taken:      {p['date_image_taken'] or 'NULL'}")
        w(f"  Project:               {p['project'] or 'None'}")
        if p.get("location"):
            w(f"  Location:              ({p['location'][0]:.6f}, {p['location'][1]:.6f})")
            w(f"  epsg_code:             {p['epsg_code'] or 'NULL'}")
        if p.get("area"):
            w(f"  Area (m2):             {p['area']:.2f}")

        # Adjudication
        adj = r["adjudication"]
        if adj:
            w(f"\nADJUDICATION:")
            if adj["classification"]:
                w(f"  Classification:        {adj['classification']}")
            if adj["species"]:
                w(f"  Species:               {adj['species']}")
            if adj["confidence"]:
                w(f"  Confidence:            {adj['confidence']}")
            if adj["review_date"]:
                w(f"  Review Date:           {adj['review_date']}")

        # Annotations
        if r["annotation_count"] is not None:
            w(f"\nANNOTATIONS:             {r['annotation_count']} reviewer(s)")

        # ETL linkage
        w(f"\n{'-'*60}")
        w("ETL LINKAGE:")
        etl = r["etl"]
        if etl["found"]:
            w(self.style.SUCCESS(
                f"  [OK] ETL Record Found (via {etl['match_type']})"))
            er = etl["record"]
            w(f"    ETL.id:              {er['id']}")
            w(f"    ETL.table_name:      {er['table_name'] or 'NULL'}")
            w(f"    ETL.vendor_id:       {er['vendor_id'] or 'NULL'}")
            w(f"    ETL.entity_id:       {er['entity_id'] or 'NULL'}")
            w(f"    ETL.date:            {er['date'] or 'NULL'}")
            w(f"    ETL.aoi_id:          {er['aoi_id'] or 'NULL'}")
            if etl["date_status"] == "mismatch":
                w(self.style.WARNING(
                    f"    [!] DATE MISMATCH: POI={p['date_image_taken']},"
                    f" ETL={er['date']}"))
            elif etl["date_status"] == "match":
                w(self.style.SUCCESS("    [OK] Dates match"))
            elif etl["date_status"] == "poi_null":
                w(self.style.WARNING(
                    f"    [!] POI date NULL but ETL has date={er['date']}"))
        else:
            w(self.style.ERROR("  [X] No ETL record found"))
            w(f"    Searched: catalog_id={p['catalog_id']}, "
              f"vendor_id={p['vendor_id']}, entity_id={p['entity_id']}")

        # Source catalogs
        w(f"\n{'-'*60}")
        w("SOURCE CATALOG LINKAGE:")
        sc = r["source_catalogs"]

        ee = sc.get("ee", {})
        if not ee.get("available", True):
            w("  - EarthExplorer:       (model not available)")
        elif ee.get("found"):
            w(self.style.SUCCESS("  [OK] EarthExplorer Record Found"))
            w(f"    EE.id:               {ee['record']['pk']}")
            w(f"    EE.vendor_id:        {ee['record']['vendor_id'] or 'NULL'}")
            w(f"    EE.entity_id:        {ee['record']['entity_id'] or 'NULL'}")
            w(f"    EE.aoi_id:           {ee['record']['aoi_id']}")
            if ee.get("aoi_name"):
                if ee["aoi_name"] == "[NOT FOUND]":
                    w(self.style.WARNING("    [!] AOI not found"))
                else:
                    w(f"    AOI Name:            {ee['aoi_name']}")
        else:
            w("  - EarthExplorer:       No match")

        gegd = sc.get("gegd", {})
        if not gegd.get("available", True):
            w("  - GEOINTDiscovery:     (model not available)")
        elif gegd.get("found"):
            w(self.style.SUCCESS("  [OK] GEOINTDiscovery Record Found"))
            w(f"    GEGD.id:             {gegd['record']['id']}")
            w(f"    GEGD.legacy_id:      {gegd['record']['legacy_id'] or 'NULL'}")
        else:
            w("  - GEOINTDiscovery:     No match")

        mgp = sc.get("mgp", {})
        if not mgp.get("available", True):
            w("  - MaxarGeospatialPlatform: (model not available)")
        elif mgp.get("found"):
            w(self.style.SUCCESS("  [OK] MaxarGeospatialPlatform Record Found"))
            w(f"    MGP.id:              {mgp['record']['id']}")
            w(f"    MGP.platform:        {mgp['record']['platform'] or 'NULL'}")
        else:
            w("  - MaxarGeospatialPlatform: No match")

        # Summary
        w(f"\n{'='*60}")
        if r["issues"]:
            w(self.style.WARNING(f"ISSUES: {', '.join(r['issues'])}"))
        else:
            w(self.style.SUCCESS("[OK] Full data lineage intact"))

    def _action_validate(self, options):
        """
        Validate POI data integrity across full chain.
        Thin wrapper — delegates to validate_poi_chain().

        Args:
            options: Parsed CLI options dict.
        """
        from animal.utils.poi_validation import validate_poi_chain

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("POI Data Validation Report (Full Chain)")
        self.stdout.write(f"{'='*60}\n")

        self.stdout.write("Checking POI -> ETL linkage...")
        self.stdout.write("Checking date consistency (POI vs ETL)...")
        self.stdout.write("Checking ETL -> Source catalog linkage...")

        result = validate_poi_chain()

        # Report
        self.stdout.write(f"\n{'-'*60}")
        self.stdout.write("VALIDATION RESULTS")
        self.stdout.write(f"{'-'*60}\n")

        if not result["issues"] and not result["warnings"]:
            self.stdout.write(self.style
                .SUCCESS("[OK] No data integrity issues found."))
        else:
            if result["issues"]:
                self.stdout.write(self.style.ERROR("ERRORS:"))
                for msg in result["issues"]:
                    self.stdout.write(self.style.ERROR(f"  [X] {msg}"))

            if result["warnings"]:
                self.stdout.write(self.style.WARNING("\nWARNINGS:"))
                for msg in result["warnings"]:
                    self.stdout.write(self.style.WARNING(f"  [!] {msg}"))

        # Chain summary table
        self.stdout.write(f"\n{'-'*60}")
        self.stdout.write("CHAIN SUMMARY")
        self.stdout.write(f"{'-'*60}\n")

        self.stdout.write(f"  {'Entity':<30} {'Count':>10}")
        self.stdout.write(f"  {'-'*30} {'-'*10}")

        # Display in chain order
        chain_order = [
            "AreaOfInterest", "EarthExplorer", "GEOINTDiscovery",
            "MaxarGeospatialPlatform", "ExtractTransformLoad",
            "PointsOfInterest",
        ]
        for entity in chain_order:
            if entity in result["chain_counts"]:
                count = result["chain_counts"][entity]
                self.stdout.write(f"  {entity:<30} {count:>10}")

        self.stdout.write("")

    def _action_repair(self, options: dict) -> None:
        """
        Repair orphan POIs with NULL catalog_id.

        --dry-run: diagnose — report POIs lacking catalog_id linkage,
        grouped by project, with annotation counts and risk assessment.
        --confirm: execute — fix NULL catalog_id via POI-to-POI matching.

        Args:
            options: Command options dict from argparse.
        """
        dry_run = options.get("dry_run")
        confirm = options.get("confirm")

        if not dry_run and not confirm:
            raise CommandError(
                "repair requires --dry-run (diagnose) or --confirm (execute)."
            )
        if dry_run and confirm:
            raise CommandError(
                "Cannot use --dry-run and --confirm together."
            )

        if dry_run:
            return self._repair_diagnose(options)
        else:
            return self._repair_execute(options)

    def _repair_diagnose(self, options: dict) -> None:
        """Diagnose POIs with NULL catalog_id (repair --dry-run).
        Thin wrapper — delegates to diagnose_null_catalog_ids()."""
        from animal.utils.poi_repair import diagnose_null_catalog_ids

        r = diagnose_null_catalog_ids()

        if r["total_null"] == 0:
            self.stdout.write(self.style.SUCCESS("[OK] No POIs with NULL catalog_id."))
            return

        w = self.stdout.write
        w(f"\n{'='*80}")
        w("NULL catalog_id DIAGNOSIS")
        w(f"{'='*80}\n")
        w(f"Total POIs with NULL catalog_id:  {r['total_null']}")
        w(f"Unique vendor_ids affected:       {len(r['all_vendor_ids'])}")
        w(f"  - Resolvable via ETL:           {r['resolvable']}")
        w(f"  - Not found in ETL:             {r['unresolvable']}")
        w(f"With annotations:                 {r['total_annotated']}")
        w(f"Adjudicated (final_* set):        {r['total_adjudicated']}")
        w(f"Reviewed (final_review_date):     {r['total_reviewed']}")
        w(f"Projects affected:                {len(r['project_stats'])}")

        w(f"\n{'-'*80}")
        w("BY PROJECT:")
        w(f"{'Project':<30} {'POIs':>8} {'Vendors':>8} {'Annotated':>10} "
          f"{'Adjudicated':>12}")
        w(f"{'-'*30} {'-'*8} {'-'*8} {'-'*10} {'-'*12}")
        for s in r["project_stats"]:
            line = (f"{s['project_label'][:30]:<30} {s['poi_count']:>8} "
                    f"{s['vendor_count']:>8} {s['annotated_count']:>10} "
                    f"{s['adjudicated_count']:>12}")
            if s["annotated_count"] > 0 or s["adjudicated_count"] > 0:
                w(self.style.WARNING(line))
            else:
                w(line)

        w(f"\n{'-'*80}")
        w("VENDOR_ID -> ETL RESOLUTION:")
        w(f"{'vendor_id':<28} {'Match':>8} {'ETL.id':<20} {'Source':>8}")
        w(f"{'-'*28} {'-'*8} {'-'*20} {'-'*8}")
        for vid in sorted(r["all_vendor_ids"]):
            res = r["vendor_resolution"].get(vid)
            if res:
                match_type, etl_id, source = res
                w(f"{vid[:28]:<28} {match_type:>8} {str(etl_id)[:20]:<20} {source:>8}")
            else:
                w(self.style.ERROR(
                    f"{vid[:28]:<28} {'NONE':>8} {'-':<20} {'-':>8}"))

        w(f"\n{'-'*80}")
        w("SAMPLE RECORDS (first 5 per project, max 3 projects):")
        w(f"  {'ID':<10} {'Project':<20} {'vendor_id':<28} {'reviewed':<12}")
        w(f"  {'-'*10} {'-'*20} {'-'*28} {'-'*12}")
        for sr in r["sample_records"]:
            reviewed_str = str(sr["reviewed"]) if sr["reviewed"] else "No"
            w(f"  {sr['id']:<10} {sr['project'][:20]:<20} "
              f"{str(sr['vendor_id'] or 'NULL')[:28]:<28} {reviewed_str[:12]:<12}")

        w(f"\n{'='*80}")
        w("DIAGNOSIS COMPLETE")
        w(f"{'='*80}")
        w(f"Action: repair --dry-run")
        w(f"Timestamp: {timezone.now().isoformat()}")
        w(f"Total NULL catalog_id: {r['total_null']}")
        w(f"Unique vendor_ids: {len(r['all_vendor_ids'])}")
        w(f"Resolvable via ETL: {r['resolvable']}")
        w(f"With annotations: {r['total_annotated']}")
        w(f"Adjudicated: {r['total_adjudicated']}")
        w(f"{'='*80}")

        if r["total_null"] > 0:
            w(f"\nTo delete these orphaned POIs:")
            w(f"  python manage.py poi delete --null-catalog-id --dry-run")
            w(f"  python manage.py poi delete --null-catalog-id --confirm")

    def _repair_execute(self, options: dict) -> None:
        """Repair orphan POIs via POI-to-POI matching (repair --confirm).
        Thin wrapper — delegates to repair_orphan_pois()."""
        from animal.utils.poi_repair import repair_orphan_pois

        verbose = options.get("verbose")
        batch_size = options.get("batch_size", 1000)

        r = repair_orphan_pois(batch_size=batch_size)

        if r["orphan_count"] == 0:
            self.stdout.write(self.style.SUCCESS(
                "[OK] No orphan POIs found (all have catalog_id)."))
            return

        w = self.stdout.write
        w(f"Orphan POIs (NULL catalog_id): {r['orphan_count']}\n")
        w(f"POI lookup table size: {r['lookup_size']}")
        w(f"Order ID patterns indexed: {r['order_patterns']}\n")

        w(f"\n{'='*60}")
        w("MATCH SUMMARY")
        w(f"{'='*60}")
        w(f"Total orphans analyzed:    {r['orphan_count']}")
        w(f"Matched by vendor_id:      {r['matched_by_vendor_id']}")
        w(f"Matched by order_id:       {r['matched_by_order_id']}")
        w(f"No match found:            {r['no_match']}")
        w(f"{'='*60}\n")

        if verbose and r["match_details"]:
            w("\nMATCH DETAILS:")
            w("-" * 80)
            for d in r["match_details"]:
                w(f"  POI {d['poi_id']}: {d['vendor_id'][:50]}...")
                w(f"    -> {d['catalog_id']} ({d['match_type']})")
            w("-" * 80)

        if r["failure_no_vendor"]:
            w(self.style.WARNING(
                f"\nWARNING: {len(r['failure_no_vendor'])} orphans have no vendor_id"))
            if verbose:
                for pid in r["failure_no_vendor"][:10]:
                    w(f"    POI {pid}")
                if len(r["failure_no_vendor"]) > 10:
                    w(f"    ... and {len(r['failure_no_vendor']) - 10} more")

        if r["failure_no_match"]:
            w(self.style.WARNING(
                f"\nWARNING: {len(r['failure_no_match'])} orphans have "
                f"vendor_id but no POI match"))
            if verbose:
                for pid, vid in r["failure_no_match"][:10]:
                    w(f"    POI {pid}: {vid}")
                if len(r["failure_no_match"]) > 10:
                    w(f"    ... and {len(r['failure_no_match']) - 10} more")

        if r["updated"] > 0:
            w(self.style.SUCCESS(f"\nSUCCESS: Repaired {r['updated']} orphan POI(s)."))
            w(f"\n{'='*60}")
            w("EXECUTION COMPLETE")
            w(f"{'='*60}")
            w(f"Action: Repair orphan POIs (POI-to-POI matching)")
            w(f"Ticket: GAIFAGP-447")
            w(f"Timestamp: {r['timestamp']}")
            w(f"Records updated: {r['updated']}")
            w(f"Remaining NULL catalog_id: {r['remaining_null']}")
            w(f"{'='*60}")
            if r["remaining_null"] > 0:
                w(self.style.WARNING(
                    f"\nWARNING: {r['remaining_null']} POIs still have NULL catalog_id.\n"
                    "   These have no matching POI with same vendor_id/order_id.\n"
                    "   Manual review required."))
        else:
            w(self.style.WARNING(
                "\nNo records updated. No POI-to-POI matches found."))

    def _action_delete(self, options: dict) -> None:
        """
        Delete POIs with safety checks and cascade preview.
        Thin wrapper — delegates to preview/execute in poi_deletion.py.
        Handler owns: arg validation, queryset building, CLI formatting.

        Args:
            options: Command options dict from argparse.
        """
        from animal.utils.poi_deletion import (
            execute_poi_deletion,
            preview_poi_deletion,
        )

        dry_run = options.get("dry_run")
        confirm = options.get("confirm")
        select_all = options.get("all")
        nuclear = options.get("nuclear")
        verbose = options.get("verbose")

        if not dry_run and not confirm:
            raise CommandError(
                "Delete requires --dry-run (to preview) or --confirm (to execute).\n"
                "Example: python manage.py poi delete --null-catalog-id --dry-run"
            )
        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")

        # Build queryset — shared filter logic, plus delete-specific --ids
        queryset = self._build_queryset(options)

        has_criteria = any(options.get(k) for k in [
            "poi_id", "null_dates", "has_dates", "catalog_id", "vendor_id",
            "entity_id", "project", "aoi", "null_catalog_id", "filters",
        ])

        if options.get("ids"):
            try:
                id_list = [int(x.strip()) for x in options["ids"].split(",")]
                queryset = queryset.filter(id__in=id_list)
                has_criteria = True
            except ValueError:
                raise CommandError(
                    "--ids must be comma-separated integers (e.g., --ids=1,2,3)")

        if not has_criteria and not select_all:
            raise CommandError(
                "Delete requires selection criteria or --all flag.\n"
                "Examples:\n"
                "  python manage.py poi delete --null-catalog-id --dry-run\n"
                "  python manage.py poi delete --id=12345 --confirm\n"
                "  python manage.py poi delete --ids=1,2,3 --dry-run\n"
                "  python manage.py poi delete --filter='project_id=5' --dry-run"
            )
        if select_all and confirm and not nuclear:
            raise CommandError(
                "Deleting ALL POIs requires the --i-really-want-to-delete-all flag.\n"
                "This prevents accidental purges of the entire POI table.\n\n"
                "To proceed:\n"
                "  python manage.py poi delete --all --confirm"
                    " --i-really-want-to-delete-all"
            )

        # Preview
        preview = preview_poi_deletion(queryset, verbose=verbose)
        count = preview["count"]

        if count == 0:
            self.stdout.write(self.style
                .WARNING("No POIs match the criteria. Nothing to delete."))
            return

        w = self.stdout.write
        w(f"\n{'='*60}")
        w(f"POIs selected for deletion: {count}")
        w(f"{'='*60}\n")

        for p in preview["preview_pois"]:
            w(f"  [{p['id']}] catalog_id={str(p['catalog_id'] or 'NULL')[:16]}, "
              f"vendor_id={str(p['vendor_id'] or 'NULL')[:20]}, "
              f"project={str(p['project'] or '(No Project)')[:20]}")
        if count > 20:
            w(f"  ... and {count - 20} more")

        w("")
        w(f"Related records (will CASCADE on delete):")
        w(f"  Annotations: {preview['related_annotations']}")

        if verbose and preview["annotation_details"]:
            w(f"\n  Annotation Details:")
            w(f"  {'ID':<8} {'POI':<8} {'Annotator':<16} {'Target':<20} "
              f"{'Class':<12} {'Confidence':<12} {'Date':<12}")
            w(f"  {'-'*8} {'-'*8} {'-'*16} {'-'*20} {'-'*12} {'-'*12} {'-'*12}")
            for a in preview["annotation_details"]:
                w(f"  {a['id']:<8} {a['poi_id']:<8} "
                  f"{(a['annotator'] or '—'):<16} {(a['target'] or '—'):<20} "
                  f"{(a['classification'] or '—'):<12} "
                  f"{(a['confidence'] or '—'):<12} {(a['date'] or '—'):<12}")
            if preview["related_annotations"] > 50:
                w(f"  ... and {preview['related_annotations'] - 50} more")

        w("")
        if preview["related_annotations"] > 0:
            w(self.style.WARNING(
                f"WARNING: {preview['related_annotations']} annotation(s) "
                f"will be deleted!"))
            if not verbose:
                w(self.style.WARNING(
                    "         Use --verbose to see annotation details."))
            w("")

        if dry_run:
            w(self.style.WARNING(
                f"[DRY RUN] Would delete {count} POI(s). No changes made."))
            if preview["related_annotations"] > 0:
                w(self.style.WARNING(
                    f"          Would also delete "
                    f"{preview['related_annotations']} annotation(s)."))
            w("\nRun with --confirm to execute.")
            return

        # Execute
        try:
            result = execute_poi_deletion(queryset)
            w(self.style.SUCCESS(f"Deleted {count} POI(s)."))
            w(f"\n{'='*60}")
            w("EXECUTION COMPLETE")
            w(f"{'='*60}")
            w(f"Action: Delete POIs")
            w(f"Timestamp: {result['timestamp']}")
            w(f"POIs deleted: {count}")
            w(f"{'='*60}")
            if result["cascade_details"]:
                w("\nDeletion details (CASCADE breakdown):")
                for model, cnt in result["cascade_details"].items():
                    w(f"  {model}: {cnt}")
        except Exception as e:
            raise CommandError(f"Delete failed: {e}")
