# ------------------------------------------------------------------------------
# ----- db_snapshot.py ---------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Pull SpatiaLite databases from Azure File Shares (dev/test/prod),
#              archive date-stamped copies, compare schemas across environments,
#              and run management commands against snapshot databases.
#
#    tickets:  GAIFAGP-549 (db_snapshot — cross-environment database validation tool)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - Azure File Shares are the live database locations
#      - gaia-storage = dev, gaia-storage-test = test, gaia-storage-prod = prod
#      - Downloaded snapshots are READ-ONLY copies — never written back
#
#    SAFETY:
#      - READ-ONLY against Azure File Shares — downloads only, never uploads
#      - All copies go to C:\gis\data\databases\YYYY.MM.DD\
#      - Never modifies the working db.sqlite3
#      - Requires --confirm to download (dry-run by default)
#      - Every operation is audit-logged to C:\gis\data\databases\logs\
#
#    REQUIRES:
#      - Azure CLI (`az`) installed and authenticated (`az login`)
#
#    usage:    python manage.py db_snapshot --help
#              python manage.py db_snapshot pull --dry-run
#              python manage.py db_snapshot pull --confirm
#              python manage.py db_snapshot pull --env=prod --confirm
#              python manage.py db_snapshot compare
#              python manage.py db_snapshot compare --env=dev --env=test
#              python manage.py db_snapshot compare --table=animal_pointsofinterest
#              python manage.py db_snapshot run --env=prod -- poi stats
#              python manage.py db_snapshot run --all -- poi stats
#              python manage.py db_snapshot list
#
# ------------------------------------------------------------------------------

import io
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone

from animal.utils.snapshot_utils import (
    ARCHIVE_ROOT,
    ENVIRONMENTS,
    ENV_ORDER,
    KEY_TABLES,
    check_az_cli,
    compare_column_types,
    compute_sha256,
    create_audit_log,
    download_database,
    get_all_table_columns,
    get_db_summary,
    get_migration_list,
    get_snapshot_path,
    get_table_schema,
    get_working_db_path,
    integrity_check,
    list_snapshots,
    resolve_snapshot_dir,
    validate_row_count_thresholds,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = """
Pull SpatiaLite databases from Azure File Shares and compare schemas.

ACTIONS:
  pull       Download db.sqlite3 from one or all environments
  compare    Compare schemas, migrations, and row counts across snapshots
  run        Execute a management command against a snapshot database
  list       List existing snapshots in the archive

EXAMPLES:
  db_snapshot pull --dry-run                              # Preview download
  db_snapshot pull --confirm                               # Download all three
  db_snapshot pull --env=prod --confirm                    # Download prod only
  db_snapshot compare                                      # Full comparison
  db_snapshot compare --env=dev --env=prod                 # Compare two only
  db_snapshot compare --table=animal_pointsofinterest      # Deep table compare
  db_snapshot compare --migration                          # Migration state only
  db_snapshot run --env=prod -- poi stats                  # Run poi stats on prod
  db_snapshot run --all -- poi stats                       # Run on all three
  db_snapshot run --env=prod -- test animal.tests -v 0     # Tests against prod
  db_snapshot list                                         # Show archive
"""

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action")

        # --- pull ---
        pull_p = subparsers.add_parser("pull", help="Download databases from Azure")
        pull_p.add_argument(
            "--env", choices=ENV_ORDER, action="append", default=None,
            help="Environment(s) to pull. Repeatable. Default: all three."
        )
        pull_p.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Preview download without pulling. This is the default behavior."
        )
        pull_p.add_argument(
            "--confirm", action="store_true",
            help="Actually download. Without this flag, dry-run only."
        )
        pull_p.add_argument(
            "--account-key", default=None,
            help="Azure storage account key. If omitted, uses az CLI default auth."
        )

        # --- compare ---
        cmp_p = subparsers.add_parser("compare", help="Compare schemas across snapshots")
        cmp_p.add_argument(
            "--env", choices=ENV_ORDER, action="append", default=None,
            help="Environment(s) to include. Repeatable. Default: all available."
        )
        cmp_p.add_argument(
            "--date", default=None,
            help="Snapshot date (YYYY.MM.DD). Default: latest."
        )
        cmp_p.add_argument(
            "--table", default=None,
            help="Deep-compare a specific table."
        )
        cmp_p.add_argument(
            "--migration", action="store_true",
            help="Compare migration state only."
        )
        cmp_p.add_argument(
            "--threshold", type=float, default=0.20,
            help="Row count change threshold (0.0-1.0). Default: 0.20 (20%%)."
        )

        # --- run ---
        run_p = subparsers.add_parser("run", help="Run command against snapshot")
        run_p.add_argument(
            "--env", choices=ENV_ORDER, action="append", default=None,
            help="Environment(s) to run against. Repeatable."
        )
        run_p.add_argument(
            "--all", action="store_true",
            help="Run against all three environments."
        )
        run_p.add_argument(
            "--date", default=None,
            help="Snapshot date (YYYY.MM.DD). Default: latest."
        )
        run_p.add_argument(
            "command_args", nargs="*",
            help="Management command and arguments (after --)."
        )

        # --- list ---
        subparsers.add_parser("list", help="List archived snapshots")

    def handle(self, *args, **options):
        action = options.get("action")
        if not action:
            self.print_help("manage.py", "db_snapshot")
            return

        handler = {
            "pull": self._action_pull,
            "compare": self._action_compare,
            "run": self._action_run,
            "list": self._action_list,
        }.get(action)

        if handler:
            handler(options)

    # -----------------------------------------------------------------------
    # PULL — thin wrapper over snapshot_utils.download_database
    # -----------------------------------------------------------------------

    def _action_pull(self, options: Dict[str, Any]) -> None:
        """
        Download databases from Azure File Shares.

        Args:
            options: Parsed command options (env, confirm, account_key).
        """
        confirm = options.get("confirm", False)
        envs = options.get("env") or ENV_ORDER
        account_key = options.get("account_key")

        today = timezone.now().strftime("%Y.%m.%d")
        dest_dir = ARCHIVE_ROOT / today

        # Pre-flight
        if confirm:
            ok, msg = check_az_cli()
            if not ok:
                raise CommandError(f"Azure CLI check failed: {msg}")
            self.stdout.write(f"  Azure account: {msg}")

        self.stdout.write(f"\n  Snapshot date:  {today}")
        self.stdout.write(f"  Archive dir:    {dest_dir}")
        self.stdout.write(f"  Environments:   {', '.join(envs)}")
        self.stdout.write(f"  Mode:           {'DOWNLOAD' if confirm else 'DRY RUN'}\n")

        # Working db path — where az downloads to before archiving
        working_db = get_working_db_path()

        if not confirm:
            for env in envs:
                cfg = ENVIRONMENTS[env]
                archive_file = dest_dir / f"{cfg['label']}.sqlite3"
                self.stdout.write(
                    f"  [DRY RUN] {cfg['share']}/{cfg['file']}"
                    f" -> {working_db} -> {archive_file}"
                )
            self.stdout.write(f"\n  Working db: {working_db}")
            self.stdout.write(f"  Last env ({envs[-1]}) stays as working copy.")
            self.stdout.write("\n  Pass --confirm to download.\n")
            return

        dest_dir.mkdir(parents=True, exist_ok=True)

        log_lines = [f"  Working db path: {working_db}"]
        results = {}
        for env in envs:
            cfg = ENVIRONMENTS[env]
            archive_file = dest_dir / f"{cfg['label']}.sqlite3"

            self.stdout.write(f"  Pulling {env} ({cfg['share']})... ", ending="")

            # Step 1: Download to working db path
            success, msg = download_database(
                share_name=cfg["share"],
                remote_path=cfg["file"],
                local_path=working_db,
                account_key=account_key,
            )

            if success:
                self.stdout.write(self.style.SUCCESS(f"OK ({msg})"))

                # Step 2: Integrity check at working path
                intact, integrity_msg = integrity_check(working_db)
                if not intact:
                    self.stdout.write(self.style.ERROR(
                        f"    INTEGRITY FAILED: {integrity_msg}"
                    ))
                    results[env] = None
                    log_lines.append(
                        f"  {env}: downloaded ({msg}) but CORRUPT. "
                        f"Details: {integrity_msg}"
                    )
                    continue

                # Step 3: SHA-256 for provenance
                sha256 = compute_sha256(working_db)
                self.stdout.write(f"    SHA-256: {sha256}")

                # Step 4: COPY to archive
                shutil.copy2(str(working_db), str(archive_file))
                self.stdout.write(f"    Archived: {archive_file}")

                results[env] = archive_file
                log_lines.append(f"  {env}: downloaded ({msg})")
                log_lines.append(f"    SHA-256: {sha256}")
                log_lines.append(f"    Archived: {archive_file}")

                summary = get_db_summary(archive_file)
                self._print_db_summary(env, summary)
                log_lines.append(
                    f"    tables={len(summary['tables'])}, "
                    f"migrations={summary['migration_count']}, "
                    f"size={summary['size_mb']:.1f}MB, "
                    f"integrity=OK"
                )
                for table, count in summary["row_counts"].items():
                    log_lines.append(f"    {table}: {count:,}")
            else:
                self.stdout.write(self.style.ERROR(f"FAILED ({msg})"))
                results[env] = None
                log_lines.append(f"  {env}: FAILED ({msg})")

        # Report what's left as working copy
        last_successful = [e for e in envs if results.get(e)]
        if last_successful:
            self.stdout.write(
                f"\n  Working db is now: {last_successful[-1]}"
            )
            log_lines.append(f"\n  Working db left as: {last_successful[-1]}")

        # Audit log
        log_path = create_audit_log("pull", envs, "\n".join(log_lines))
        self.stdout.write(f"\n  Audit log: {log_path}\n")

    def _print_db_summary(self, env: str, summary: Dict[str, Any]) -> None:
        """Format and print database summary stats."""
        self.stdout.write(f"\n  --- {env.upper()} ---")
        self.stdout.write(f"    Tables: {len(summary['tables'])}")
        self.stdout.write(f"    Animal migrations: {summary['migration_count']}")
        for table, count in summary["row_counts"].items():
            short_name = table.replace("animal_", "").replace("auth_", "")
            self.stdout.write(f"    {short_name}: {count:,}")

    # -----------------------------------------------------------------------
    # COMPARE — schema, migration, and row count comparison
    # -----------------------------------------------------------------------

    def _action_compare(self, options: Dict[str, Any]) -> None:
        """
        Compare schemas across snapshot databases.

        Args:
            options: Parsed command options (env, date, table, migration).
        """
        date_str = options.get("date")
        table_filter = options.get("table")
        check_migrations = options.get("migration", False)
        env_filter = options.get("env")

        snap_dir = resolve_snapshot_dir(date_str)
        if not snap_dir:
            raise CommandError(
                "No snapshot found. Run 'db_snapshot pull --confirm' first."
            )

        self.stdout.write(f"\n  Comparing snapshot: {snap_dir.name}\n")

        # Find available databases
        available_envs = env_filter or ENV_ORDER
        db_paths = {}
        for env in available_envs:
            cfg = ENVIRONMENTS[env]
            db_path = snap_dir / f"{cfg['label']}.sqlite3"
            if db_path.exists():
                db_paths[env] = db_path
            else:
                self.stdout.write(
                    f"  {env}: {self.style.WARNING('not found')}"
                )

        if len(db_paths) < 2:
            raise CommandError("Need at least 2 databases to compare.")

        log_lines = []

        if check_migrations or not table_filter:
            migration_output = self._compare_migrations(db_paths)
            log_lines.append(migration_output)

        threshold = options.get("threshold", 0.20)

        if table_filter:
            table_output = self._compare_table(db_paths, table_filter)
            log_lines.append(table_output)
        else:
            schema_output = self._compare_schemas(db_paths)
            log_lines.append(schema_output)
            counts_output = self._compare_row_counts(db_paths, threshold)
            log_lines.append(counts_output)

        # Audit log
        log_path = create_audit_log(
            "compare", list(db_paths.keys()), "\n".join(log_lines)
        )
        self.stdout.write(f"\n  Audit log: {log_path}\n")

    def _compare_migrations(self, db_paths: Dict[str, Path]) -> str:
        """Compare Django migration state. Returns log text."""
        self.stdout.write("  === Migration State ===\n")
        lines = ["=== Migration State ==="]

        migrations = {}
        for env, path in db_paths.items():
            migrations[env] = get_migration_list(path)

        ref_env = max(migrations, key=lambda e: len(migrations[e]))

        for env, migs in migrations.items():
            msg = f"    {env}: {len(migs)} animal migrations"
            self.stdout.write(msg)
            lines.append(msg)

        ref_set = set(migrations.get(ref_env, []))
        for env in ENV_ORDER:
            if env == ref_env or env not in migrations:
                continue
            env_set = set(migrations[env])
            missing = ref_set - env_set
            extra = env_set - ref_set
            if missing:
                self.stdout.write(
                    f"\n    {env} MISSING (in {ref_env} but not {env}):"
                )
                lines.append(f"\n  {env} MISSING (in {ref_env}):")
                for m in sorted(missing):
                    self.stdout.write(f"      - {m}")
                    lines.append(f"    - {m}")
            if extra:
                self.stdout.write(
                    f"\n    {env} EXTRA (in {env} but not {ref_env}):"
                )
                lines.append(f"\n  {env} EXTRA (not in {ref_env}):")
                for m in sorted(extra):
                    self.stdout.write(f"      + {m}")
                    lines.append(f"    + {m}")

        if all(
            set(migrations[e]) == ref_set
            for e in migrations
            if e != ref_env
        ):
            msg = "    All environments at same migration level."
            self.stdout.write(self.style.SUCCESS(f"\n{msg}"))
            lines.append(msg)

        self.stdout.write("")
        return "\n".join(lines)

    def _compare_schemas(self, db_paths: Dict[str, Path]) -> str:
        """Compare table presence and column definitions. Returns log text."""
        self.stdout.write("  === Schema Comparison ===\n")
        lines = ["=== Schema Comparison ==="]

        schemas = {}
        for env, path in db_paths.items():
            schemas[env] = get_all_table_columns(path)

        all_tables = set()
        for env_tables in schemas.values():
            all_tables.update(env_tables.keys())

        table_diff = False
        for table in sorted(all_tables):
            present = [e for e in ENV_ORDER if e in schemas and table in schemas[e]]
            missing = [e for e in ENV_ORDER if e in schemas and table not in schemas[e]]
            if missing:
                table_diff = True
                msg = (
                    f"    {table}: present in {', '.join(present)}"
                    f" -- MISSING from {', '.join(missing)}"
                )
                self.stdout.write(msg)
                lines.append(msg)

        if not table_diff:
            msg = "    All tables present in all environments."
            self.stdout.write(msg)
            lines.append(msg)

        # Column diffs for animal_ tables
        animal_tables = sorted(t for t in all_tables if t.startswith("animal_"))
        col_diffs = []
        for table in animal_tables:
            env_cols = {}
            for env in ENV_ORDER:
                if env in schemas and table in schemas[env]:
                    env_cols[env] = schemas[env][table]
            if len(env_cols) < 2:
                continue

            ref_env = "dev" if "dev" in env_cols else list(env_cols.keys())[0]
            ref_col_names = set(c[0] for c in env_cols[ref_env])

            for env, cols in env_cols.items():
                if env == ref_env:
                    continue
                env_col_names = set(c[0] for c in cols)
                missing = ref_col_names - env_col_names
                extra = env_col_names - ref_col_names
                if missing or extra:
                    col_diffs.append((table, env, missing, extra))

        if col_diffs:
            self.stdout.write("\n    Column differences:")
            lines.append("\n  Column differences:")
            for table, env, missing, extra in col_diffs:
                if missing:
                    msg = (
                        f"      {table} -- {env} MISSING: "
                        f"{', '.join(sorted(missing))}"
                    )
                    self.stdout.write(msg)
                    lines.append(msg)
                if extra:
                    msg = (
                        f"      {table} -- {env} EXTRA: "
                        f"{', '.join(sorted(extra))}"
                    )
                    self.stdout.write(msg)
                    lines.append(msg)
        else:
            msg = "    All animal_ table columns match across environments."
            self.stdout.write(msg)
            lines.append(msg)

        # Column type mismatches (same column name, different declared type)
        envs_list = [e for e in ENV_ORDER if e in schemas]
        ref_env = envs_list[0]
        type_diffs_found = False
        for env in envs_list[1:]:
            type_mismatches = compare_column_types(schemas[ref_env], schemas[env])
            if type_mismatches:
                if not type_diffs_found:
                    self.stdout.write("\n    Column type mismatches:")
                    lines.append("\n  Column type mismatches:")
                    type_diffs_found = True
                for table, col, type_a, type_b in type_mismatches:
                    msg = (
                        f"      {table}.{col}: "
                        f"{ref_env}={type_a}, {env}={type_b}"
                    )
                    self.stdout.write(msg)
                    lines.append(msg)

        if not type_diffs_found:
            msg = "    All column types match across environments."
            self.stdout.write(msg)
            lines.append(msg)

        self.stdout.write("")
        return "\n".join(lines)

    def _compare_row_counts(
        self, db_paths: Dict[str, Path], threshold: float = 0.20
    ) -> str:
        """Compare row counts for key tables. Returns log text."""
        self.stdout.write("  === Row Counts ===\n")
        lines = ["=== Row Counts ==="]

        envs = [e for e in ENV_ORDER if e in db_paths]
        header = f"    {'Table':<40s}" + "".join(f"{e:>10s}" for e in envs)
        separator = "    " + "-" * (40 + 10 * len(envs))
        self.stdout.write(header)
        self.stdout.write(separator)
        lines.extend([header, separator])

        # Collect counts by env for threshold validation
        env_counts = {env: {} for env in envs}

        for table in KEY_TABLES:
            counts = {}
            for env in envs:
                schema = get_table_schema(db_paths[env], table)
                val = schema["row_count"] if schema else None
                counts[env] = val
                if val is not None:
                    env_counts[env][table] = val

            row = f"    {table:<40s}"
            for env in envs:
                val = counts.get(env)
                if val is None:
                    row += f"{'N/A':>10s}"
                else:
                    row += f"{val:>10,d}"
            self.stdout.write(row)
            lines.append(row)

        # Threshold validation — pairwise comparisons
        warnings_found = False
        for i, env_a in enumerate(envs):
            for env_b in envs[i + 1:]:
                alerts = validate_row_count_thresholds(
                    env_counts[env_a], env_counts[env_b], threshold=threshold
                )
                if alerts:
                    if not warnings_found:
                        self.stdout.write(
                            f"\n    Row count alerts (>{threshold:.0%} change):"
                        )
                        lines.append(
                            f"\n  Row count alerts (>{threshold:.0%} change):"
                        )
                        warnings_found = True
                    self.stdout.write(f"\n    {env_a} vs {env_b}:")
                    lines.append(f"\n  {env_a} vs {env_b}:")
                    for alert in alerts:
                        self.stdout.write(f"      {alert}")
                        lines.append(f"    {alert}")

        if not warnings_found:
            msg = f"\n    All row counts within {threshold:.0%} tolerance."
            self.stdout.write(msg)
            lines.append(msg)

        self.stdout.write("")
        return "\n".join(lines)

    def _compare_table(self, db_paths: Dict[str, Path], table_name: str) -> str:
        """Deep comparison of a single table. Returns log text."""
        self.stdout.write(f"  === Table: {table_name} ===\n")
        lines = [f"=== Table: {table_name} ==="]

        for env in ENV_ORDER:
            if env not in db_paths:
                continue

            schema = get_table_schema(db_paths[env], table_name)
            if not schema:
                msg = f"    {env}: table not found"
                self.stdout.write(msg)
                lines.append(msg)
                continue

            self.stdout.write(f"    {env}:")
            lines.append(f"  {env}:")

            msg = f"      Rows: {schema['row_count']:,}"
            self.stdout.write(msg)
            lines.append(msg)

            self.stdout.write(f"      Columns ({len(schema['columns'])}):")
            lines.append(f"    Columns ({len(schema['columns'])}):")
            for col in schema["columns"]:
                nullable = " NULL" if col["nullable"] else ""
                pk = " PK" if col["pk"] else ""
                msg = f"        {col['name']:<30s} {col['type']:<15s}{nullable}{pk}"
                self.stdout.write(msg)
                lines.append(msg)

            if schema["indexes"]:
                self.stdout.write(f"      Indexes ({len(schema['indexes'])}):")
                lines.append(f"    Indexes ({len(schema['indexes'])}):")
                for idx in schema["indexes"]:
                    unique = "UNIQUE " if idx["unique"] else ""
                    msg = f"        {unique}{idx['name']}: {', '.join(idx['columns'])}"
                    self.stdout.write(msg)
                    lines.append(msg)

            self.stdout.write("")

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # RUN — execute management command against a snapshot database
    # -----------------------------------------------------------------------

    def _action_run(self, options: Dict[str, Any]) -> None:
        """
        Run a management command against snapshot database(s).

        Temporarily swaps Django's default database connection to point
        at the snapshot, runs the command, then restores the original.
        The snapshot is never modified — this is read-only.

        Args:
            options: Parsed command options (env, all, date, command_args).
        """
        run_all = options.get("all", False)
        env_filter = options.get("env")
        date_str = options.get("date")
        command_args = options.get("command_args") or []

        if not command_args:
            raise CommandError(
                "No command specified. Usage: db_snapshot run --env=prod -- poi stats"
            )

        if run_all:
            envs = ENV_ORDER
        elif env_filter:
            envs = env_filter
        else:
            raise CommandError("Specify --env=<env> or --all.")

        snap_dir = resolve_snapshot_dir(date_str)
        if not snap_dir:
            raise CommandError(
                "No snapshot found. Run 'db_snapshot pull --confirm' first."
            )

        cmd_name = command_args[0]
        cmd_args = command_args[1:]

        self.stdout.write(f"\n  Snapshot: {snap_dir.name}")
        self.stdout.write(f"  Command:  manage.py {' '.join(command_args)}")
        self.stdout.write(f"  Targets:  {', '.join(envs)}\n")

        log_lines = [
            f"Command: manage.py {' '.join(command_args)}",
            f"Snapshot: {snap_dir.name}",
            f"Targets: {', '.join(envs)}",
        ]

        for env in envs:
            cfg = ENVIRONMENTS[env]
            db_path = snap_dir / f"{cfg['label']}.sqlite3"

            if not db_path.exists():
                msg = f"  {env}: snapshot not found, skipping"
                self.stdout.write(self.style.WARNING(msg))
                log_lines.append(f"\n{'=' * 40}\n{env.upper()}: SKIPPED (not found)")
                continue

            self.stdout.write(self.style.HTTP_INFO(
                f"  {'=' * 60}"
            ))
            self.stdout.write(self.style.HTTP_INFO(
                f"  {env.upper()} ({db_path.name})"
            ))
            self.stdout.write(self.style.HTTP_INFO(
                f"  {'=' * 60}"
            ))

            # Capture output
            output = self._run_against_snapshot(db_path, cmd_name, cmd_args)
            self.stdout.write(output)

            log_lines.append(f"\n{'=' * 40}\n{env.upper()}:\n{output}")

        # Audit log
        log_path = create_audit_log("run", envs, "\n".join(log_lines))
        self.stdout.write(f"\n  Audit log: {log_path}\n")

    def _run_against_snapshot(
        self, db_path: Path, cmd_name: str, cmd_args: List[str]
    ) -> str:
        """
        Execute a management command with Django's database pointed at a snapshot.

        Swaps the default database connection NAME to the snapshot path,
        runs the command, captures stdout/stderr, then restores the original.

        Args:
            db_path: Path to the snapshot .sqlite3 file.
            cmd_name: Management command name (e.g., 'poi').
            cmd_args: Command arguments (e.g., ['stats']).

        Returns:
            Captured stdout+stderr as a string.
        """
        original_db = connections.databases["default"]["NAME"]
        output_buffer = io.StringIO()

        # Detach the live connection wrapper instead of closing it.
        # This preserves in-memory test databases that would be destroyed
        # by close(). The wrapper is held aside and restored in finally.
        original_wrapper = getattr(connections._connections, "default", None)
        if original_wrapper is not None:
            delattr(connections._connections, "default")

        try:
            # Point Django at the snapshot — next access creates a new wrapper
            connections.databases["default"]["NAME"] = str(db_path)

            call_command(cmd_name, *cmd_args, stdout=output_buffer, stderr=output_buffer)

        except Exception as e:
            output_buffer.write(f"\nERROR: {e}\n")
            logger.exception(
                "db_snapshot run failed",
                extra={"db_path": str(db_path), "command": cmd_name},
            )
        finally:
            # Close the snapshot connection (if one was opened)
            try:
                connections["default"].close()
            except Exception:
                pass

            # Restore original database path and connection wrapper
            connections.databases["default"]["NAME"] = original_db
            if original_wrapper is not None:
                setattr(connections._connections, "default", original_wrapper)

        return output_buffer.getvalue()

    # -----------------------------------------------------------------------
    # LIST — show archived snapshots
    # -----------------------------------------------------------------------

    def _action_list(self, options: Dict[str, Any]) -> None:
        """List existing snapshot archives."""
        snapshots = list_snapshots()

        if not snapshots:
            self.stdout.write("  No snapshots found.")
            return

        self.stdout.write(f"\n  Archive: {ARCHIVE_ROOT}\n")
        self.stdout.write(
            f"  {'Date':<15s} {'dev':<15s} {'test':<15s} {'prod':<15s}"
        )
        self.stdout.write("  " + "-" * 60)

        for snap in snapshots:
            row = f"  {snap['date']:<15s}"
            for env in ENV_ORDER:
                size = snap["envs"].get(env)
                if size is not None:
                    row += f"{size:.1f} MB".ljust(15)
                else:
                    row += "---".ljust(15)
            self.stdout.write(row)

        self.stdout.write("")
