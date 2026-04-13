"""
Post-backfill audit for GAIFAGP-563.

Run after each environment's backfill to verify data quality.
Reports: row counts, column-level NULL/empty population rates,
per-catalog_id EE/ETL/POI cross-reference, orphan POI count,
and nullable field inspection for the 5 target catalog_ids.

Discovers columns from the live schema — no hardcoded column lists.

Usage:
    python manage.py audit_ee_563
"""
# ----------------------------------------------------------------------
# ----- audit_ee_563.py ------------------------------------------------
# ----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  One-time audit — not a permanent command.
#              Verifies EE backfill data quality for 5 WV-2
#              catalog_ids after backfill_ee_563 execution.
#
#    tickets:  GAIFAGP-563 (EE backfill for WV-2 CCB acquisitions)
#
# ----------------------------------------------------------------------

from django.core.management.base import BaseCommand
from django.db import connection


TARGET_CATALOG_IDS = [
    "10300100BB27E800",
    "10300100BBB08100",
    "10300100BC063D00",
    "10300100BC254B00",
    "10300100BCAF4B00",
]

ALL_CATALOG_IDS = TARGET_CATALOG_IDS + [
    "1040010065796C00",
    "1040010066A1F000",
    "1040010066A36E00",
    "1040010067015600",
    "10400100674B2100",
    "1040010067CC5D00",
    "1040010067D36B00",
    "10400100687EDC00",
    "10400100918AEE00",
    "1040010093A5AA00",
]


class Command(BaseCommand):
    help = (
        "Post-backfill audit for GAIFAGP-563. "
        "Reports EE/ETL population, cross-reference, "
        "orphan counts, and nullable field inspection."
    )

    def handle(self, *args, **options):
        w = self.stdout.write
        cur = connection.cursor()

        # --- Table audits ---
        self._col_audit(w, cur, "animal_earthexplorer")
        self._col_audit(w, cur, "etl")

        # --- Cross-reference ---
        self._catalog_id_xref(w, cur, ALL_CATALOG_IDS)

        # --- Orphan count ---
        orphans = self._orphan_count(w, cur)

        # --- Nullable field inspection for target catalog_ids ---
        self._inspect_nullable_fields(w, cur)

        # --- Result ---
        if orphans == 0:
            w(self.style.SUCCESS("\nPASS — zero orphan POIs"))
        else:
            w(self.style.ERROR(
                f"\nFAIL — {orphans} orphan POIs remain"
            ))

    def _get_columns(self, cur, table):
        """Discover column names from PRAGMA table_info."""
        cur.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]

    def _col_audit(self, w, cur, table):
        """Print column-level NULL and empty-string counts."""
        columns = self._get_columns(cur, table)

        cur.execute(f"SELECT COUNT(*) FROM {table}")
        total = cur.fetchone()[0]

        w(f"\n{'='*65}")
        w(f"{table} — {total} rows")
        w(f"{'='*65}")
        w(f"{'Column':<28} {'Total':>6} {'NULL':>6} "
          f"{'Empty':>6} {'Pop%':>6}")
        w(f"{'-'*65}")

        for c in columns:
            cur.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE [{c}] IS NULL"
            )
            nulls = cur.fetchone()[0]
            cur.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE [{c}] = ''"
            )
            empties = cur.fetchone()[0]
            populated = total - nulls - empties
            pct = (
                (populated / total * 100) if total else 0
            )
            w(f"{c:<28} {total:>6} {nulls:>6} "
              f"{empties:>6} {pct:>5.1f}%")

    def _catalog_id_xref(self, w, cur, catalog_ids):
        """Cross-reference EE, ETL, and POI counts."""
        w(f"\n{'='*55}")
        w("Catalog ID Cross-Reference")
        w(f"{'='*55}")
        w(f"{'catalog_id':<20} {'EE':>5} {'ETL':>5} "
          f"{'POIs':>7}")
        w(f"{'-'*55}")

        for cid in catalog_ids:
            cur.execute(
                "SELECT COUNT(*) FROM animal_earthexplorer "
                "WHERE catalog_id = ?", [cid]
            )
            ee = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM etl WHERE id = ?",
                [cid]
            )
            etl = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM "
                "animal_pointsofinterest "
                "WHERE catalog_id = ?", [cid]
            )
            poi = cur.fetchone()[0]
            w(f"{cid:<20} {ee:>5} {etl:>5} {poi:>7}")

    def _orphan_count(self, w, cur):
        """Count POIs with catalog_id but no matching EE record."""
        cur.execute(
            "SELECT COUNT(*) FROM animal_pointsofinterest p "
            "WHERE p.catalog_id IS NOT NULL "
            "AND p.catalog_id NOT IN "
            "(SELECT catalog_id FROM animal_earthexplorer)"
        )
        count = cur.fetchone()[0]
        w(f"\n{'='*40}")
        w(f"Orphan POIs (catalog_id, no EE): {count}")
        w(f"{'='*40}")
        return count

    def _inspect_nullable_fields(self, w, cur):
        """Inspect nullable fields on the 5 target catalog_ids."""
        w(f"\n{'='*118}")
        w("Nullable Field Inspection (target catalog_ids)")
        w(f"{'='*118}")
        w(f"{'entity_id':<28} {'catalog_id':<18} "
          f"{'acq_date':<12} {'entered':<12} "
          f"{'published':<12} {'lic_uplift':<12} "
          f"{'event_dt':<12}")
        w(f"{'-'*118}")

        placeholders = ",".join(
            ["?"] * len(TARGET_CATALOG_IDS)
        )
        cur.execute(
            f"SELECT entity_id, catalog_id, "
            f"acquisition_date, date_entered, "
            f"publish_date, license_uplift_update, "
            f"event_date "
            f"FROM animal_earthexplorer "
            f"WHERE catalog_id IN ({placeholders}) "
            f"ORDER BY catalog_id, entity_id",
            TARGET_CATALOG_IDS,
        )

        for row in cur.fetchall():
            vals = [
                str(v) if v is not None else "NULL"
                for v in row
            ]
            w(f"{vals[0]:<28} {vals[1]:<18} "
              f"{vals[2]:<12} {vals[3]:<12} "
              f"{vals[4]:<12} {vals[5]:<12} "
              f"{vals[6]:<12}")
