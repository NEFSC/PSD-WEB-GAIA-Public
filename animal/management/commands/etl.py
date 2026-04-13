# ------------------------------------------------------------------------------
# ----- etl.py -----------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Unified management command for ExtractTransformLoad operations.
#              Supports listing, inspecting, and validating ETL records.
#              Primary use case: diagnosing POI backfill failures by inspecting
#              the source of truth for acquisition dates.
#
#    tickets:  GAIFAGP-290 (supports POI date backfill diagnostics)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - ETL table is authoritative for imagery acquisition metadata
#      - ETL.id is the catalog_id used by POI records
#      - ETL.date is the source for POI.date_image_taken
#      - ETL records are created during imagery ingestion and should not
#        be modified manually (read-only operations in this command)
#
#    usage:    python manage.py etl --help
#              python manage.py etl stats
#              python manage.py etl list --null-dates --limit=50
#              python manage.py etl list --id=1030010012345678
#              python manage.py etl inspect --id=1030010012345678
#              python manage.py etl validate
#              python manage.py etl orphans
#
# ------------------------------------------------------------------------------

import argparse
import json
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from animal.models import ExtractTransformLoad, PointsOfInterest
from animal.utils.utils import TeeWriter


class Command(BaseCommand):
    help = """
Inspect and validate ExtractTransformLoad (ETL) records in the GAIA database.

SOURCE OF TRUTH:
  ETL table is authoritative for imagery acquisition metadata.
  ETL.id = catalog_id used by POI records.
  ETL.date = source for POI.date_image_taken backfill.

ACTIONS:
  stats       Show summary statistics for ETL table
  list        List ETL records with optional filtering
  inspect     Show detailed information for specific ETL record(s)
  validate    Check data integrity and report issues
  count       Count ETL records matching criteria
  orphans     Find ETL records with no linked POIs (or vice versa)

EXAMPLES:
  # Show ETL table statistics
  python manage.py etl stats

  # List ETL records with NULL dates (problematic for backfill)
  python manage.py etl list --null-dates

  # List ETL records for a specific catalog_id
  python manage.py etl list --id=1030010012345678

  # Search by vendor_id pattern
  python manage.py etl list --vendor-id=WV03

  # Inspect a specific ETL record with full detail
  python manage.py etl inspect --id=1030010012345678

  # Validate ETL data integrity
  python manage.py etl validate

  # Find orphaned records (ETL without POIs or POIs without ETL)
  python manage.py etl orphans

  # Export to JSON for analysis
  python manage.py etl list --format=json --limit=1000 --output=etl_export.json

DIAGNOSTIC USE CASE:
  When POI backfill fails to match records, use this command to:
  1. Check if the catalog_id exists in ETL: etl list --id=<catalog_id>
  2. Check if ETL.date is NULL: etl list --null-dates
  3. Find orphaned references: etl orphans
  4. Validate overall integrity: etl validate

NOTE:
  This command is READ-ONLY. ETL records are created during imagery
  ingestion and should not be modified manually.
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
            choices=["stats", "list", "inspect", "validate", "count", "orphans"],
            help="Action to perform"
        )

        # Selection/filtering
        selection = parser.add_argument_group("Selection Criteria")
        selection.add_argument(
            "--id",
            type=str,
            dest="etl_id",
            help="Filter by ETL id (catalog_id)"
        )
        selection.add_argument(
            "--ids",
            type=str,
            help="Filter by comma-separated IDs"
        )
        selection.add_argument(
            "--vendor-id",
            type=str,
            help="Filter by vendor_id (partial match)"
        )
        selection.add_argument(
            "--entity-id",
            type=str,
            help="Filter by entity_id (partial match)"
        )
        selection.add_argument(
            "--null-dates",
            action="store_true",
            help="Select only records where date is NULL"
        )
        selection.add_argument(
            "--has-dates",
            action="store_true",
            help="Select only records where date is populated"
        )
        selection.add_argument(
            "--filter",
            type=str,
            action="append",
            dest="filters",
            help="Django ORM filter (e.g., --filter='sensor=WV03'). Can be repeated."
        )
        selection.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Limit number of records shown (default: 100)"
        )

        # Output formatting
        output = parser.add_argument_group("Output Options")
        output.add_argument(
            "--format",
            choices=["simple", "table", "csv", "json", "jira"],
            default="simple",
            help="Output format (default: simple)"
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

            if action == "stats":
                self._action_stats(options)
            elif action == "list":
                self._action_list(options)
            elif action == "inspect":
                self._action_inspect(options)
            elif action == "validate":
                self._action_validate(options)
            elif action == "count":
                self._action_count(options)
            elif action == "orphans":
                self._action_orphans(options)
        finally:
            if output_file:
                output_file.close()
                self.stdout = original_stdout

    def _build_queryset(self, options):
        """Build ETL queryset based on selection criteria."""
        qs = ExtractTransformLoad.objects.all()

        if options.get("etl_id"):
            qs = qs.filter(id=options["etl_id"])

        if options.get("ids"):
            id_list = [x.strip() for x in options["ids"].split(",")]
            qs = qs.filter(id__in=id_list)

        if options.get("vendor_id"):
            qs = qs.filter(vendor_id__icontains=options["vendor_id"])

        if options.get("entity_id"):
            qs = qs.filter(entity_id__icontains=options["entity_id"])

        if options.get("null_dates"):
            qs = qs.filter(date__isnull=True)

        if options.get("has_dates"):
            qs = qs.filter(date__isnull=False)

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

    def _action_stats(self, options):
        """Show summary statistics for ETL table."""
        total = ExtractTransformLoad.objects.count()
        with_dates = ExtractTransformLoad.objects.filter(date__isnull=False).count()
        null_dates = ExtractTransformLoad.objects.filter(date__isnull=True).count()

        # Date range
        from django.db.models import Min, Max
        date_stats = ExtractTransformLoad.objects.filter(date__isnull=False).aggregate(
            min_date=Min('date'),
            max_date=Max('date')
        )

        # Count by sensor/platform if field exists
        try:
            by_sensor = (
                ExtractTransformLoad.objects
                .values("sensor")
                .annotate(count=Count("id"))
                .order_by("-count")[:10]
            )
            has_sensor = True
        except Exception:
            by_sensor = []
            has_sensor = False

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("ExtractTransformLoad (ETL) Table Statistics")
        self.stdout.write(f"{'='*60}\n")

        self.stdout.write(f"Total records:       {total:>10,}")
        self.stdout.write(f"With date:           {with_dates:>10,} ({100*with_dates/total if total else 0:.1f}%)")
        self.stdout.write(f"NULL date:           {null_dates:>10,} ({100*null_dates/total if total else 0:.1f}%)")

        if date_stats['min_date'] or date_stats['max_date']:
            self.stdout.write(f"\nDate Range:")
            self.stdout.write(f"  Earliest:          {date_stats['min_date']}")
            self.stdout.write(f"  Latest:            {date_stats['max_date']}")

        if has_sensor and by_sensor:
            self.stdout.write(f"\nBy Sensor (top 10):")
            self.stdout.write(f"  {'Sensor':<20} {'Count':>10}")
            self.stdout.write(f"  {'-'*20} {'-'*10}")
            for row in by_sensor:
                sensor = row["sensor"] or "(NULL)"
                self.stdout.write(f"  {sensor:<20} {row['count']:>10,}")

        # POI linkage summary
        poi_total = PointsOfInterest.objects.count()
        poi_with_catalog = PointsOfInterest.objects.filter(catalog_id__isnull=False).count()
        
        self.stdout.write(f"\nPOI Linkage:")
        self.stdout.write(f"  Total POIs:        {poi_total:>10,}")
        self.stdout.write(f"  With catalog_id:   {poi_with_catalog:>10,}")

        if null_dates > 0:
            self.stdout.write(
                self.style.WARNING(f"\n⚠️  {null_dates:,} ETL records have NULL dates.")
            )
            self.stdout.write("   These cannot be used for POI date backfill.")
            self.stdout.write("   Run: python manage.py etl list --null-dates")

    def _action_list(self, options):
        """List ETL records with optional filtering."""
        qs = self._build_queryset(options)
        limit = options["limit"]
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.WARNING("No ETL records match criteria."))
            return

        fmt = options["format"]

        if fmt == "json":
            data = list(
                qs.order_by("id")[:limit].values(
                    "id", "vendor_id", "entity_id", "date", "sensor"
                )
            )
            # Convert dates to strings for JSON serialization
            for row in data:
                if row.get("date"):
                    row["date"] = str(row["date"])
            self.stdout.write(json.dumps(data, indent=2))
            return

        if fmt == "csv":
            self.stdout.write("id,vendor_id,entity_id,date,sensor")
            for etl in qs.order_by("id")[:limit]:
                date_str = str(etl.date) if etl.date else ""
                self.stdout.write(f"{etl.id},{etl.vendor_id or ''},{etl.entity_id or ''},{date_str},{getattr(etl, 'sensor', '') or ''}")
            return

        self.stdout.write(f"\nShowing {min(limit, total)} of {total:,} ETL record(s):\n")

        if fmt == "table":
            self.stdout.write(f"{'ID':<22} {'Vendor ID':<20} {'Date':<12} {'Sensor':<10}")
            self.stdout.write("-" * 66)

        if fmt == "jira":
            self.stdout.write("||ID||Vendor ID||Date||Sensor||")

        for etl in qs.order_by("id")[:limit]:
            date_str = str(etl.date) if etl.date else "NULL"
            sensor = getattr(etl, 'sensor', None) or "N/A"
            vendor = etl.vendor_id or "N/A"

            if fmt == "simple":
                null_marker = " ⚠️" if etl.date is None else ""
                self.stdout.write(f"  [{etl.id}] vendor={vendor} | date={date_str}{null_marker}")
            elif fmt == "table":
                self.stdout.write(f"{etl.id:<22} {vendor:<20} {date_str:<12} {sensor:<10}")
            elif fmt == "jira":
                self.stdout.write(f"|{etl.id}|{vendor}|{date_str}|{sensor}|")

        if total > limit:
            self.stdout.write(f"\n... {total - limit:,} more. Use --limit to see more.")

    def _action_inspect(self, options):
        """Show detailed information for selected ETL record(s)."""
        if not options.get("etl_id") and not options.get("ids"):
            raise CommandError("Inspect requires --id or --ids to specify record(s).")

        qs = self._build_queryset(options)
        count = qs.count()

        if count == 0:
            self.stdout.write(self.style.WARNING("No ETL records match criteria."))
            return

        if count > 10:
            raise CommandError(f"Too many records ({count}). Narrow your criteria or use list action.")

        for etl in qs:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"ETL Record: {etl.id}")
            self.stdout.write(f"{'='*60}")
            
            self.stdout.write(f"\nIdentifiers:")
            self.stdout.write(f"  id (catalog_id):   {etl.id}")
            self.stdout.write(f"  vendor_id:         {etl.vendor_id or 'NULL'}")
            self.stdout.write(f"  entity_id:         {etl.entity_id or 'NULL'}")

            self.stdout.write(f"\nAcquisition:")
            date_str = str(etl.date) if etl.date else "NULL"
            self.stdout.write(f"  date:              {date_str}")
            if hasattr(etl, 'sensor'):
                self.stdout.write(f"  sensor:            {etl.sensor or 'NULL'}")

            # Find linked POIs using type-normalized comparison
            etl_id_str = str(etl.id)
            linked_pois = PointsOfInterest.objects.filter(catalog_id=etl_id_str).count()
            
            # Also check vendor_id and entity_id linkage
            vendor_linked = 0
            entity_linked = 0
            if etl.vendor_id:
                vendor_linked = PointsOfInterest.objects.filter(vendor_id=str(etl.vendor_id)).count()
            if etl.entity_id:
                entity_linked = PointsOfInterest.objects.filter(entity_id=str(etl.entity_id)).count()

            self.stdout.write(f"\nPOI Linkage:")
            self.stdout.write(f"  By catalog_id:     {linked_pois}")
            self.stdout.write(f"  By vendor_id:      {vendor_linked}")
            self.stdout.write(f"  By entity_id:      {entity_linked}")

            if etl.date is None:
                self.stdout.write(
                    self.style.WARNING("\n⚠️  This record has NULL date - cannot be used for POI backfill!")
                )

            if linked_pois == 0 and vendor_linked == 0 and entity_linked == 0:
                self.stdout.write(
                    self.style.WARNING("\n⚠️  No POIs reference this ETL record (orphaned source data)")
                )

    def _action_validate(self, options):
        """Validate ETL data integrity."""
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("ETL Data Validation Report")
        self.stdout.write(f"{'='*60}\n")

        issues = []
        warnings = []

        # Check for NULL dates
        null_dates = ExtractTransformLoad.objects.filter(date__isnull=True).count()
        if null_dates > 0:
            warnings.append(f"{null_dates:,} records with NULL date (cannot backfill POIs)")

        # Check for NULL vendor_id
        null_vendor = ExtractTransformLoad.objects.filter(vendor_id__isnull=True).count()
        if null_vendor > 0:
            warnings.append(f"{null_vendor:,} records with NULL vendor_id")

        # Check for duplicate IDs (should never happen, but verify)
        from django.db.models import Count as DjCount
        dupes = (
            ExtractTransformLoad.objects
            .values('id')
            .annotate(count=DjCount('id'))
            .filter(count__gt=1)
        )
        dupe_count = dupes.count()
        if dupe_count > 0:
            issues.append(f"{dupe_count:,} duplicate ETL IDs detected (data integrity error!)")

        # Check POI linkage - POIs with catalog_id not in ETL
        poi_catalog_ids = set(
            str(cid) for cid in
            PointsOfInterest.objects.filter(catalog_id__isnull=False)
            .values_list("catalog_id", flat=True).distinct()
        )
        etl_ids = set(
            str(eid) for eid in
            ExtractTransformLoad.objects.values_list("id", flat=True)
        )
        orphaned_pois = poi_catalog_ids - etl_ids
        if orphaned_pois:
            warnings.append(f"{len(orphaned_pois):,} POI catalog_ids not found in ETL table")

        # Report
        total_etl = ExtractTransformLoad.objects.count()
        total_poi = PointsOfInterest.objects.count()

        self.stdout.write(f"Records checked:")
        self.stdout.write(f"  ETL records:       {total_etl:>10,}")
        self.stdout.write(f"  POI records:       {total_poi:>10,}")
        self.stdout.write("")

        if not issues and not warnings:
            self.stdout.write(self.style.SUCCESS("✓ No data integrity issues found."))
        else:
            if issues:
                self.stdout.write(self.style.ERROR("ERRORS:"))
                for issue in issues:
                    self.stdout.write(self.style.ERROR(f"  ✗ {issue}"))
                self.stdout.write("")

            if warnings:
                self.stdout.write(self.style.WARNING("WARNINGS:"))
                for warning in warnings:
                    self.stdout.write(self.style.WARNING(f"  ⚠ {warning}"))

        self.stdout.write("")

    def _action_count(self, options):
        """Count ETL records matching criteria."""
        qs = self._build_queryset(options)
        count = qs.count()

        if options.get("quiet"):
            self.stdout.write(str(count))
        else:
            self.stdout.write(f"ETL count: {count:,}")

    def _action_orphans(self, options):
        """Find orphaned records - ETL without POIs or POIs without ETL."""
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("Orphan Analysis: ETL ↔ POI Linkage")
        self.stdout.write(f"{'='*60}\n")

        # Get all IDs as strings for comparison
        etl_ids = set(
            str(eid) for eid in
            ExtractTransformLoad.objects.values_list("id", flat=True)
        )
        
        poi_catalog_ids = set(
            str(cid) for cid in
            PointsOfInterest.objects.filter(catalog_id__isnull=False)
            .values_list("catalog_id", flat=True).distinct()
        )

        # ETL records with no POI references
        etl_without_poi = etl_ids - poi_catalog_ids
        
        # POI catalog_ids with no ETL record
        poi_without_etl = poi_catalog_ids - etl_ids

        self.stdout.write(f"Summary:")
        self.stdout.write(f"  Total ETL records:                 {len(etl_ids):>10,}")
        self.stdout.write(f"  Total unique POI catalog_ids:      {len(poi_catalog_ids):>10,}")
        self.stdout.write(f"  ETL records with no POI linkage:   {len(etl_without_poi):>10,}")
        self.stdout.write(f"  POI catalog_ids not in ETL:        {len(poi_without_etl):>10,}")

        limit = options["limit"]
        fmt = options["format"]

        if etl_without_poi:
            self.stdout.write(f"\n{'-'*60}")
            self.stdout.write(f"ETL Records with No POI Linkage (showing up to {limit}):")
            self.stdout.write(f"{'-'*60}")
            
            orphan_etls = ExtractTransformLoad.objects.filter(
                id__in=list(etl_without_poi)[:limit]
            )
            
            for etl in orphan_etls[:limit]:
                date_str = str(etl.date) if etl.date else "NULL"
                self.stdout.write(f"  {etl.id} | date={date_str}")

            if len(etl_without_poi) > limit:
                self.stdout.write(f"  ... and {len(etl_without_poi) - limit:,} more")

        if poi_without_etl:
            self.stdout.write(f"\n{'-'*60}")
            self.stdout.write(f"POI catalog_ids Not Found in ETL (showing up to {limit}):")
            self.stdout.write(f"{'-'*60}")
            
            for catalog_id in list(poi_without_etl)[:limit]:
                poi_count = PointsOfInterest.objects.filter(catalog_id=catalog_id).count()
                self.stdout.write(f"  {catalog_id} ({poi_count} POI(s))")

            if len(poi_without_etl) > limit:
                self.stdout.write(f"  ... and {len(poi_without_etl) - limit:,} more")

        if not etl_without_poi and not poi_without_etl:
            self.stdout.write(
                self.style.SUCCESS("\n✓ All records are properly linked. No orphans found.")
            )
        else:
            self.stdout.write("")
            if poi_without_etl:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️  {len(poi_without_etl):,} POI catalog_ids cannot be backfilled "
                        "(no matching ETL record)"
                    )
                )
