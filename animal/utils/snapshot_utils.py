# ------------------------------------------------------------------------------
# ----- snapshot_utils.py ------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Business logic for Azure File Share database snapshot operations.
#              Downloads SpatiaLite databases from dev/test/prod, compares
#              schemas and migration state, and provides connection swapping
#              for running management commands against remote snapshots.
#
#    tickets:  GAIFAGP-549 (db_snapshot — cross-environment database validation tool)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - Azure File Shares are the live database locations
#      - gaia-storage = dev, gaia-storage-test = test, gaia-storage-prod = prod
#      - db.sqlite3 is the database filename on all three shares
#      - Downloaded snapshots are READ-ONLY copies — never written back
#
# ------------------------------------------------------------------------------

import hashlib
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STORAGE_ACCOUNT = ""
ARCHIVE_ROOT = Path(r"C:\gis\data\databases")
LOG_DIR = ARCHIVE_ROOT / "logs"

ENVIRONMENTS = {
    "dev":  {"share": "gaia-storage",      "file": "db.sqlite3", "label": "db_dev"},
    "test": {"share": "gaia-storage-test", "file": "db.sqlite3", "label": "db_test"},
    "prod": {"share": "gaia-storage-prod", "file": "db.sqlite3", "label": "db_prod"},
}

ENV_ORDER = ["dev", "test", "prod"]

# Tables to include in row count comparisons
KEY_TABLES = [
    "animal_pointsofinterest",
    "animal_earthexplorer",
    "animal_areasofinterest",
    "animal_annotation",
    "animal_project",
    "animal_specieslocation",
    "animal_fishnet",
    "auth_user",
]


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def init_log_dir() -> Path:
    """
    Ensure the log directory exists.

    Returns:
        Path to the log directory.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def create_audit_log(action: str, envs: List[str], details: str) -> Path:
    """
    Write a timestamped audit log entry.

    Args:
        action: The db_snapshot action performed (pull, compare, run, list).
        envs: List of environment names involved.
        details: Full text output to log.

    Returns:
        Path to the created log file.
    """
    init_log_dir()
    ts = timezone.now().strftime("%Y.%m.%d_%H.%M.%S")
    env_str = "_".join(envs)
    filename = f"{ts}_{env_str}_{action}.log"
    log_path = LOG_DIR / filename

    header = (
        f"db_snapshot audit log\n"
        f"action:      {action}\n"
        f"environments: {', '.join(envs)}\n"
        f"timestamp:   {timezone.now().isoformat()}\n"
        f"operator:    {_get_operator()}\n"
        f"{'=' * 72}\n\n"
    )

    log_path.write_text(header + details, encoding="utf-8")
    logger.info(
        "Audit log written",
        extra={"action": action, "envs": envs, "log_path": str(log_path)},
    )
    return log_path


def _get_operator() -> str:
    """Return the current OS username for audit trail."""
    import os
    return os.environ.get("USERNAME", os.environ.get("USER", "unknown"))


# ---------------------------------------------------------------------------
# Azure CLI operations
# ---------------------------------------------------------------------------

def check_az_cli() -> Tuple[bool, str]:
    """
    Verify az CLI is installed and authenticated.

    Returns:
        Tuple of (success, account_name_or_error_message).
    """
    try:
        result = subprocess.run(
            ["az", "account", "show", "--query", "name", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=True,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except FileNotFoundError:
        return False, "az CLI not found"
    except subprocess.TimeoutExpired:
        return False, "az CLI timed out"


def download_database(
    share_name: str,
    remote_path: str,
    local_path: Path,
    account_key: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Download a file from Azure File Share using az CLI.

    Args:
        share_name: Azure File Share name (e.g., 'gaia-storage').
        remote_path: Path within the share (e.g., 'db.sqlite3').
        local_path: Local destination path.
        account_key: Optional storage account key. Uses az default auth if omitted.

    Returns:
        Tuple of (success, message).
    """
    cmd = [
        "az", "storage", "file", "download",
        "--share-name", share_name,
        "--path", remote_path,
        "--dest", str(local_path),
        "--account-name", STORAGE_ACCOUNT,
        "--output", "none",
    ]
    if account_key:
        cmd.extend(["--account-key", account_key])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=True,
        )
        if result.returncode != 0:
            return False, f"az error: {result.stderr.strip()}"
        if local_path.exists():
            size_mb = local_path.stat().st_size / (1024 * 1024)
            return True, f"{size_mb:.1f} MB"
        return False, "File not found after download"
    except subprocess.TimeoutExpired:
        return False, "Timeout (120s)"
    except FileNotFoundError:
        return False, "az CLI not found"


def get_working_db_path() -> Path:
    """
    Get the path to Django's working db.sqlite3 from connection settings.

    This is the live database path that Django uses at runtime
    (e.g., C:\\gis\\PSD-WEB-GAIA\\db.sqlite3 on the Azure VM).

    Returns:
        Path to the working database file.
    """
    from django.db import connections
    return Path(connections.databases["default"]["NAME"])


# ---------------------------------------------------------------------------
# Database introspection
# ---------------------------------------------------------------------------

def get_db_summary(db_path: Path) -> Dict[str, Any]:
    """
    Extract summary statistics from a SpatiaLite database.

    Args:
        db_path: Path to the .sqlite3 file.

    Returns:
        Dict with keys: tables, migration_count, row_counts, size_mb,
        integrity_ok, sha256.

    Raises:
        sqlite3.Error: If the database cannot be read.
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        # Table list
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND sql NOT LIKE 'CREATE VIRTUAL%%' "
            "ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        # Migration count
        migration_count = 0
        if "django_migrations" in tables:
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = 'animal'"
            )
            migration_count = cursor.fetchone()[0]

        # Row counts for key tables
        row_counts = {}
        for table in KEY_TABLES:
            if table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
                row_counts[table] = cursor.fetchone()[0]

        size_mb = db_path.stat().st_size / (1024 * 1024)
    finally:
        conn.close()

    # Integrity and provenance (outside the connection — integrity_check opens its own)
    integrity_ok, _ = integrity_check(db_path)
    sha256 = compute_sha256(db_path)

    return {
        "tables": tables,
        "migration_count": migration_count,
        "row_counts": row_counts,
        "size_mb": size_mb,
        "integrity_ok": integrity_ok,
        "sha256": sha256,
    }


def get_migration_list(db_path: Path) -> List[str]:
    """
    Get ordered list of animal app migrations from a database.

    Args:
        db_path: Path to the .sqlite3 file.

    Returns:
        List of migration names in application order.
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT name FROM django_migrations "
            "WHERE app = 'animal' ORDER BY id"
        )
        return [row[0] for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_table_schema(db_path: Path, table_name: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed schema information for a single table.

    Args:
        db_path: Path to the .sqlite3 file.
        table_name: Name of the table to inspect.

    Returns:
        Dict with keys: columns, indexes, row_count. None if table not found.
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    try:
        cursor.execute(f"PRAGMA table_info([{table_name}])")
        cols = cursor.fetchall()
        if not cols:
            return None

        cursor.execute(f"SELECT COUNT(*) FROM [{table_name}]")
        row_count = cursor.fetchone()[0]

        cursor.execute(f"PRAGMA index_list([{table_name}])")
        raw_indexes = cursor.fetchall()
        indexes = []
        for idx in raw_indexes:
            cursor.execute(f"PRAGMA index_info([{idx[1]}])")
            idx_cols = [r[2] for r in cursor.fetchall()]
            indexes.append({
                "name": idx[1],
                "unique": bool(idx[2]),
                "columns": idx_cols,
            })

        return {
            "columns": [
                {
                    "name": c[1],
                    "type": c[2],
                    "nullable": not c[3],
                    "pk": bool(c[5]),
                }
                for c in cols
            ],
            "indexes": indexes,
            "row_count": row_count,
        }
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def get_all_table_columns(db_path: Path) -> Dict[str, List[Tuple[str, str]]]:
    """
    Get column names and types for all tables in a database.

    Args:
        db_path: Path to the .sqlite3 file.

    Returns:
        Dict mapping table_name -> list of (column_name, column_type) tuples.
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND sql NOT LIKE 'CREATE VIRTUAL%%' "
            "ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        result = {}
        for table in tables:
            try:
                cursor.execute(f"PRAGMA table_info([{table}])")
                result[table] = [(row[1], row[2]) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                logger.debug(
                    "Skipping table during column introspection",
                    extra={"table": table},
                )
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Database integrity and provenance
# ---------------------------------------------------------------------------

def integrity_check(db_path: Path) -> Tuple[bool, str]:
    """
    Run SQLite integrity and foreign key checks on a database file.

    Args:
        db_path: Path to the .sqlite3 file.

    Returns:
        Tuple of (passed, message). passed is True only if both
        PRAGMA integrity_check and PRAGMA foreign_key_check return clean.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
    except sqlite3.Error as e:
        msg = f"Cannot open database: {e}"
        logger.warning(
            "Integrity check failed to open database",
            extra={"db_path": str(db_path), "error": str(e)},
        )
        return False, msg

    try:
        # PRAGMA integrity_check returns "ok" on success, or error descriptions
        cursor.execute("PRAGMA integrity_check;")
        integrity_rows = [row[0] for row in cursor.fetchall()]
        integrity_ok = len(integrity_rows) == 1 and integrity_rows[0] == "ok"

        # PRAGMA foreign_key_check returns empty result set on success
        cursor.execute("PRAGMA foreign_key_check;")
        fk_rows = cursor.fetchall()
        fk_ok = len(fk_rows) == 0

        if integrity_ok and fk_ok:
            msg = "ok"
            logger.info(
                "Integrity check passed",
                extra={"db_path": str(db_path)},
            )
            return True, msg

        # Build failure message
        parts = []
        if not integrity_ok:
            parts.append(f"integrity_check: {'; '.join(integrity_rows)}")
        if not fk_ok:
            fk_details = [
                f"table={r[0]}, rowid={r[1]}, parent={r[2]}, fkid={r[3]}"
                for r in fk_rows[:10]  # Cap at 10 to avoid log flood
            ]
            if len(fk_rows) > 10:
                fk_details.append(f"... and {len(fk_rows) - 10} more")
            parts.append(f"foreign_key_check: {'; '.join(fk_details)}")

        msg = " | ".join(parts)
        logger.warning(
            "Integrity check failed",
            extra={"db_path": str(db_path), "details": msg},
        )
        return False, msg

    except sqlite3.Error as e:
        msg = f"Error during integrity check: {e}"
        logger.warning(
            "Integrity check raised exception",
            extra={"db_path": str(db_path), "error": str(e)},
        )
        return False, msg
    finally:
        conn.close()


def compute_sha256(db_path: Path) -> str:
    """
    Compute SHA-256 hash of a database file for provenance tracking.

    Args:
        db_path: Path to the .sqlite3 file.

    Returns:
        Hex digest string (64 characters).

    Raises:
        FileNotFoundError: If db_path does not exist.
        OSError: If the file cannot be read.
    """
    h = hashlib.sha256()
    with open(db_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Cross-environment comparison utilities
# ---------------------------------------------------------------------------

def validate_row_count_thresholds(
    counts_a: Dict[str, int],
    counts_b: Dict[str, int],
    threshold: float = 0.20,
) -> List[str]:
    """
    Compare row counts between two environments and flag large deltas.

    A table going from >0 to 0 is always an error regardless of threshold.
    A table going from 0 to >0 is informational, not an error.

    Args:
        counts_a: Row counts from environment A (e.g., previous snapshot).
        counts_b: Row counts from environment B (e.g., current snapshot).
        threshold: Maximum acceptable proportional change (default 0.20 = 20%).

    Returns:
        List of warning/error strings. Empty list means all within tolerance.
    """
    warnings = []
    all_tables = set(counts_a.keys()) | set(counts_b.keys())

    for table in sorted(all_tables):
        a = counts_a.get(table, 0)
        b = counts_b.get(table, 0)

        # Table dropped to zero — always an error
        if a > 0 and b == 0:
            warnings.append(
                f"ERROR: {table} went from {a:,} to 0 rows (complete data loss)"
            )
            continue

        # Table appeared from zero — informational
        if a == 0 and b > 0:
            continue

        # Both zero — nothing to compare
        if a == 0 and b == 0:
            continue

        # Proportional change
        delta = abs(b - a) / a
        if delta > threshold:
            direction = "increased" if b > a else "decreased"
            warnings.append(
                f"WARNING: {table} {direction} from {a:,} to {b:,} "
                f"({delta:.0%} change, threshold {threshold:.0%})"
            )

    return warnings


def compare_column_types(
    schema_a: Dict[str, List[Tuple[str, str]]],
    schema_b: Dict[str, List[Tuple[str, str]]],
) -> List[Tuple[str, str, str, str]]:
    """
    Find column type mismatches between two schema snapshots.

    Only compares columns that exist in both schemas for a given table.
    Missing columns are not reported here (that's handled by column
    presence checks elsewhere).

    Args:
        schema_a: Output of get_all_table_columns() for environment A.
        schema_b: Output of get_all_table_columns() for environment B.

    Returns:
        List of (table_name, column_name, type_a, type_b) tuples
        for every column where the declared type differs.
    """
    mismatches = []
    common_tables = set(schema_a.keys()) & set(schema_b.keys())

    for table in sorted(common_tables):
        cols_a = {name: ctype for name, ctype in schema_a[table]}
        cols_b = {name: ctype for name, ctype in schema_b[table]}
        common_cols = set(cols_a.keys()) & set(cols_b.keys())

        for col in sorted(common_cols):
            if cols_a[col] != cols_b[col]:
                mismatches.append((table, col, cols_a[col], cols_b[col]))

    return mismatches


# ---------------------------------------------------------------------------
# Snapshot directory operations
# ---------------------------------------------------------------------------

def resolve_snapshot_dir(date_str: Optional[str] = None) -> Optional[Path]:
    """
    Find the snapshot directory for a given date, or the latest.

    Args:
        date_str: Optional date in YYYY.MM.DD format. If None, returns latest.

    Returns:
        Path to snapshot directory, or None if not found.
    """
    if date_str:
        candidate = ARCHIVE_ROOT / date_str
        return candidate if candidate.exists() else None

    if not ARCHIVE_ROOT.exists():
        return None

    snapshots = sorted(
        [d for d in ARCHIVE_ROOT.iterdir() if d.is_dir() and d.name != "logs"],
        reverse=True,
    )
    return snapshots[0] if snapshots else None


def get_snapshot_path(env: str, date_str: Optional[str] = None) -> Optional[Path]:
    """
    Get the path to a specific environment's snapshot database.

    Args:
        env: Environment name ('dev', 'test', or 'prod').
        date_str: Optional snapshot date. If None, uses latest.

    Returns:
        Path to the .sqlite3 file, or None if not found.
    """
    snap_dir = resolve_snapshot_dir(date_str)
    if not snap_dir:
        return None

    cfg = ENVIRONMENTS.get(env)
    if not cfg:
        return None

    db_path = snap_dir / f"{cfg['label']}.sqlite3"
    return db_path if db_path.exists() else None


def list_snapshots() -> List[Dict[str, Any]]:
    """
    List all archived snapshots with file sizes.

    Returns:
        List of dicts with keys: date, envs (dict of env -> size_mb or None).
    """
    if not ARCHIVE_ROOT.exists():
        return []

    snapshots = sorted(
        [d for d in ARCHIVE_ROOT.iterdir() if d.is_dir() and d.name != "logs"],
        reverse=True,
    )

    results = []
    for snap_dir in snapshots:
        envs = {}
        for env in ENV_ORDER:
            cfg = ENVIRONMENTS[env]
            db_path = snap_dir / f"{cfg['label']}.sqlite3"
            if db_path.exists():
                envs[env] = db_path.stat().st_size / (1024 * 1024)
            else:
                envs[env] = None
        results.append({"date": snap_dir.name, "envs": envs})

    return results
