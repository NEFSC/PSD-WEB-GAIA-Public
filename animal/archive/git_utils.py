# ------------------------------------------------------------------------------
#
# Download or update, after 15 days of cloning, a copy of Polar Geospatial
#   Center's "Image Utils" repository ensuring that the most up-to-date copy
#   of the repository is present for the GAIA application.
#
# This should likely be called as part of a start-up script.
#
# Written by John Wall (john.wall@noaa.gov)
#
# ------------------------------------------------------------------------------

# ----------------------------
# Import some libraries, configure Django
# ----------------------------
import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# fcntl is Unix only; provide graceful fallback on other platforms
try:  # pragma: no cover - platform specific
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore

# ----------------------------
# Defined variables
# ----------------------------
REPO_URL = "https://github.com/PolarGeospatialCenter/imagery_utils.git"
MAX_AGE_DAYS = 15

# ----------------------------
# Key functions
# ----------------------------
def _repo_age_days(path: Path) -> float:
    """Return age in days using .git mtime; large number if unknown."""
    try:
        git_dir = path / ".git"
        if not git_dir.exists():
            return 10_000  # force treat as stale
        last_modified = datetime.fromtimestamp(git_dir.stat().st_mtime)
        return (datetime.now() - last_modified).total_seconds() / 86400.0
    except Exception:
        return 10_000


def is_repo_stale(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """Determine if repo clone at path is missing or older than max_age_days."""
    if not path.exists():
        return True
    return _repo_age_days(path) > max_age_days


def _acquire_lock(lock_path: Path):
    """Acquire (and yield) an inter-process file lock. No-op if fcntl absent."""
    class _Lock:
        def __init__(self, fp):
            self.fp = fp
        def __enter__(self):
            if fcntl is not None:
                fcntl.flock(self.fp, fcntl.LOCK_EX)
            return self.fp
        def __exit__(self, exc_type, exc, tb):
            if fcntl is not None:
                fcntl.flock(self.fp, fcntl.LOCK_UN)
            try:
                self.fp.close()
            except Exception:
                pass
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = open(lock_path, "w")
    return _Lock(fp)


def clone_imagery_utils(external_dir: Path, *, force: bool = False) -> None:
    """Ensure a fresh, non-partially-cloned copy of imagery_utils is present.

    Concurrency-safe:
      * Uses an inter-process file lock (.imagery_utils.lock)
      * Clones into a temp dir then atomically renames
      * Only one process performs a (re)clone; others skip

    Parameters
    ----------
    external_dir : Path
        Target directory for the repo (e.g. animal/external/imagery_utils)
    force : bool
        If True, ignore staleness check and reclone.
    """
    if os.getenv("GAIA_SKIP_REPO_UPDATE") == "1":
        print("GAIA_SKIP_REPO_UPDATE=1; skipping imagery_utils update.")
        return

    lock_file = external_dir.parent / ".imagery_utils.lock"
    with _acquire_lock(lock_file):
        # Re-evaluate staleness while holding the lock
        stale = force or is_repo_stale(external_dir)
        if not stale:
            print(f"imagery_utils present and fresh (age={_repo_age_days(external_dir):.2f}d).")
            return

        tmp_parent = external_dir.parent
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_parent) as tmpdir:
            tmp_path = Path(tmpdir) / "imagery_utils"
            print(f"Cloning imagery_utils (stale or missing) into temp dir {tmp_path} ...")
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", REPO_URL, str(tmp_path)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                print("ERROR: git clone failed; leaving existing copy untouched.")
                print(f"stdout: {e.stdout}\nstderr: {e.stderr}")
                return

            # Successful clone; replace existing directory atomically
            backup_dir = None
            if external_dir.exists():
                backup_dir = external_dir.parent / (external_dir.name + ".bak")
                try:
                    if backup_dir.exists():
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    external_dir.rename(backup_dir)
                except Exception as e:
                    print(f"Warning: could not backup existing repo: {e}; attempting direct replace.")
                    shutil.rmtree(external_dir, ignore_errors=True)

            try:
                tmp_path.rename(external_dir)
                print(f"imagery_utils updated at {external_dir}.")
                if backup_dir and backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception as e:
                print(f"ERROR: failed finalizing new repo: {e}")
                # Attempt rollback
                if backup_dir and backup_dir.exists() and not external_dir.exists():
                    backup_dir.rename(external_dir)
                raise

# ----------------------------
# MAIN
# ----------------------------
if __name__ == "__main__":
    try:
        base_dir = Path(__file__).resolve().parent.parent
    except NameError:
        # For interactive environments or if __file__ is not available
        base_dir = Path(os.getenv("PROJECT_ROOT", Path.cwd()))

    external_dir = base_dir / "external" / "imagery_utils"

    clone_imagery_utils(external_dir)

    sys.path.append(str(external_dir))
    import pgc_ortho