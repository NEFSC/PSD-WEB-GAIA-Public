# POI Command Regression Test Suite

**Ticket:** GAIFAGP-484
**Author:** John Wall
**Last updated:** February 12, 2026

---

## 1. Purpose

Validate the entire `poi` management command capability stack end-to-end
after code changes to `poi.py`, `api_utils.py`, and `poi_backfill.py`.
Confirm that formatting-only changes (line wraps, docstrings) and
behavioral changes (timeout constants, retry policy, renamed functions)
introduce no regressions in output content, filter semantics, guard
paths, or write-action correctness.

### What is covered

| Surface | Tests | What is validated |
|---------|-------|-------------------|
| Read-only actions (stats, count, list, summary, inspect, validate, diagnose-nulls) | 52 | Query results, output structure, filter intersection semantics, CSV/table/jira format fidelity |
| Dry-run write actions (backfill-dates, backfill-identifiers, repair-orphans, delete) | 34 | CLI flag wiring, guard path rejection (all 13 CommandError paths), mock-verified function dispatch |
| Confirmed write actions | 5 | Backfill-dates fills exactly 1 fixture POI and leaves 2 others NULL; delete removes correct records |
| Behavioral changes (api_utils/poi_backfill integration) | 6 | Renamed function import, error string propagation, sensor map forwarding, flag passthrough |
| Deprecated action | 1 | `backfill-ee-metadata` raises CommandError with redirect message |

All 24 CLI flags exercised.  All 13 CommandError guard paths asserted.
186 assertions across 96 tests.  Every filter test asserts exact
expected counts against fixture constants — no "non-empty" checks.

### What is NOT covered

| Surface | Reason |
|---------|--------|
| SpatiaLite insert triggers (EE → ETL) | ETL fixtures are created via raw SQL, not trigger propagation; trigger testing requires a separate suite |
| Annotation/adjudication audit trail | Out of scope for GAIFAGP-484; POI command does not touch annotations |
| Celery task integration | POI command is synchronous; Celery coverage belongs to pipeline reliability tests |
| USGS API live calls | All backfill functions are mocked at the command import path |
| Geometry/spatial operations | AOI traversal is tested via EE fixture linkage, but no spatial intersection queries are exercised |

---

## 2. Preconditions

Complete all of these before running the suite.

### 2.1 Migration state is current and clean

```
python manage.py showmigrations animal
```

Every migration should show `[X]`.  If any show `[ ]`, run
`python manage.py migrate` on your local development database first.

```
git status animal/migrations/
```

No new or modified files.  If you see unexpected changes, do not
commit them — investigate first.

### 2.2 SpatiaLite is configured

`SPATIALITE_LIBRARY_PATH` must be set in `.env` or `settings.py`,
pointing to `mod_spatialite.dll` (Windows) or `mod_spatialite.so`
(Linux).

### 2.3 Migration 0015 blocker is resolved

Migration `0015` calls `loaddata` on a fixture referencing
`animal_target.id=9` before the Target rows exist in the test
database.  This blocks ALL tests with:

```
IntegrityError: animal_species_locations.species_id contains a
value '9' that does not have a corresponding value in animal_target.id
```

**Fix (recommended):** Edit `animal/migrations/0015_...py` so
`load_species_locations_fixture` creates the required Target rows
before calling `loaddata`, or move the `loaddata` call to a later
migration.  Commit the fix.

**Workaround:** Create `gaia/settings_test.py`:

```python
from gaia.settings import *
MIGRATION_MODULES = {"animal": None}
```

Then run with `--settings=gaia.settings_test`.  This skips migrations
entirely (Django creates tables from current models).  The trade-off is
that migration compatibility is not tested.

### 2.4 Test database is isolated

Django creates a throwaway in-memory database
(`file:memorydb_default?mode=memory&cache=shared`) for the test run.
It does not read from or write to `db.sqlite3`.  Your local database,
Azure Dev, Test, and Prod are not affected.

---

## 3. Run Steps

### Full suite

```
python manage.py test animal.tests.test_poi_command -v2
```

### Single test class

```
python manage.py test animal.tests.test_poi_command.TestPoiCount -v2
```

### Single test method

```
python manage.py test animal.tests.test_poi_command.TestPoiCount.test_T03_count_all -v2
```

### With artifact output (before/after diffing)

```
set POI_TEST_ARTIFACTS=C:\gis\test_artifacts\before
python manage.py test animal.tests.test_poi_command -v2
REM ... replace source files ...
set POI_TEST_ARTIFACTS=C:\gis\test_artifacts\after
python manage.py test animal.tests.test_poi_command -v2
diff -ru C:\gis\test_artifacts\before C:\gis\test_artifacts\after
```

Or set `POI_TEST_ARTIFACTS` in `settings.py` for persistent
configuration.  Artifact output is normalized (timestamps and
durations replaced with stable placeholders) so diffs reflect only
functional changes.

---

## 4. Expected Results

A passing run looks like this:

| Criterion | Expected |
|-----------|----------|
| Tests discovered | 96 |
| Test database | `file:memorydb_default?mode=memory&cache=shared` |
| IntegrityErrors during setup | 0 |
| Test result | All 96 PASS (no failures, no errors) |
| Skipped tests | 0 if EE model and GEOS are available; up to 3 if not (AOI traversal tests skip gracefully) |
| New migration files generated | 0 — verify with `git status animal/migrations/` after the run |
| Runtime | Under 30 seconds (typical; may vary by machine) |

### Post-run verification

After the test run completes:

```
git status animal/migrations/
```

If this shows any new or modified migration files, the test run did
not cause them — but something else on your system did.  Investigate
before committing.

---

## 5. Failure Handling

| Symptom | Cause | Resolution |
|---------|-------|------------|
| `IntegrityError` during migration 0015 | Fixture references `Target.id=9` before Target rows exist | See §2.3 — fix migration or use test settings workaround |
| `TypeError: unexpected keyword argument 'aoi'` in EE fixture | EarthExplorer FK field named `aoi_id` not `aoi` | Change `aoi=self.aoi` to `aoi_id=self.aoi` in `_create_fixtures()` |
| `OperationalError: no such module: mod_spatialite` | SpatiaLite extension not configured | Set `SPATIALITE_LIBRARY_PATH` in `.env` or `settings.py` |
| Fewer than 96 tests discovered | Test class or method removed/renamed | Compare `grep -c "def test_" test_poi_command.py` against expected count |
| Tests pass locally but would fail in CI | Local migration state differs from Git | Run `git status animal/migrations/` and `showmigrations` to verify alignment |
| Artifact diffs show unexpected differences | New timestamp or nondeterministic output pattern | Add regex to `_NOISY_PATTERNS` in `test_poi_command.py` |
| `OperationalError: no such table: etl` | `_ensure_etl_table()` fallback failed | Check that raw SQL in `setUpClass` matches current ETL schema |

---

## Files

| File | Location | Purpose |
|------|----------|---------|
| `test_poi_command.py` | `animal/tests/` | 96 regression tests |
| `REGRESSION_TESTING_README.md` | `animal/tests/` | This file |