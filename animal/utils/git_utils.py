# ------------------------------------------------------------------------------
# ----- git_utils.py -----------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    created:  2025-12 (original)
#    revised:  2026-01-21 (external directory outside project)
#
#    purpose:  Clone and manage Polar Geospatial Center's imagery_utils repo.
#              Ensures pgc_ortho.py and related tools are available for
#              calibration workflows.
#
#    tickets:  GAIFAGP-439 (imagery.py CLI)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - imagery_utils is an external dependency, NOT part of GAIA codebase
#      - External dir lives OUTSIDE the git repo to avoid tracking/locking
#      - MAX_AGE_DAYS (15) balances freshness vs clone overhead
#      - pgc_ortho.py existence is the authoritative "repo is valid" check
#      - GAIA_SKIP_REPO_UPDATE=1 disables auto-update (for offline/CI)
#
#    configuration:
#      - GAIA_EXTERNAL_DIR: Base directory for external dependencies
#        - Windows default: C:/gis/external/
#        - Linux default: /opt/gaia/external/
#      - imagery_utils cloned to: {GAIA_EXTERNAL_DIR}/imagery_utils/
#
#    usage:
#        from animal.utils.git_utils import clone_imagery_utils, get_external_dir
#        
#        # Use default location
#        external_dir = get_external_dir() / "imagery_utils"
#        clone_imagery_utils(external_dir)
#        
#        # Or use helper that handles everything
#        from animal.utils.git_utils import ensure_imagery_utils
#        path = ensure_imagery_utils()  # Returns path to imagery_utils dir
#
# ------------------------------------------------------------------------------

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from django.utils import timezone
from typing import Optional, Tuple

# fcntl is Unix-only; provide graceful fallback on Windows
try:
    import fcntl  # type: ignore
except ImportError:
    fcntl = None  # type: ignore


# ------------------------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------------------------

try:
    from animal.utils.logging import get_animal_logger
    logger = get_animal_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------

# PGC imagery_utils
PGC_REPO_URL = "https://github.com/PolarGeospatialCenter/imagery_utils.git"
PGC_REQUIRED_FILE = "pgc_ortho.py"

# Microsoft whales (generate_interesting_points)
WHALES_REPO_URL = "https://github.com/microsoft/whales.git"
WHALES_REQUIRED_FILE = "generate_interesting_points.py"

# Legacy alias
REPO_URL = PGC_REPO_URL
REQUIRED_FILE = PGC_REQUIRED_FILE

MAX_AGE_DAYS = 15

# Platform-specific default external directories
# These are OUTSIDE the git repo to avoid tracking and file locking issues
DEFAULT_EXTERNAL_DIR_WINDOWS = Path("C:/gis/external")
DEFAULT_EXTERNAL_DIR_LINUX = Path("/opt/gaia/external")


# ------------------------------------------------------------------------------
# External Directory Configuration
# ------------------------------------------------------------------------------

def get_external_dir() -> Path:
    """
    Get the base directory for external dependencies.
    
    Resolution order:
    1. GAIA_EXTERNAL_DIR environment variable (if set)
    2. Platform-specific default:
       - Windows: C:/gis/external/
       - Linux/macOS: /opt/gaia/external/
    
    Returns:
        Path to external dependencies directory.
        
    Note:
        The directory will be created if it doesn't exist.
        imagery_utils will be cloned to {external_dir}/imagery_utils/
    """
    # Check environment variable first
    env_dir = os.getenv("GAIA_EXTERNAL_DIR")
    if env_dir:
        external_dir = Path(env_dir)
        logger.debug(f"[GIT] Using GAIA_EXTERNAL_DIR: {external_dir}")
    elif platform.system() == "Windows":
        external_dir = DEFAULT_EXTERNAL_DIR_WINDOWS
        logger.debug(f"[GIT] Using Windows default: {external_dir}")
    else:
        external_dir = DEFAULT_EXTERNAL_DIR_LINUX
        logger.debug(f"[GIT] Using Linux default: {external_dir}")
    
    # Ensure directory exists
    try:
        external_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logger.warning(f"[GIT] Cannot create {external_dir}, falling back to user directory")
        # Fallback to user's home directory
        external_dir = Path.home() / ".gaia" / "external"
        external_dir.mkdir(parents=True, exist_ok=True)
    
    return external_dir


def get_imagery_utils_dir() -> Path:
    """
    Get the directory where imagery_utils should be cloned.
    
    Returns:
        Path to imagery_utils directory (may not exist yet).
    """
    return get_external_dir() / "imagery_utils"


# ------------------------------------------------------------------------------
# Repository Validation
# ------------------------------------------------------------------------------

def _check_repo_status(path: Path) -> Tuple[bool, bool, float, str]:
    """
    Comprehensive repository status check.
    
    Args:
        path: Path to repository root (e.g., /opt/gaia/external/imagery_utils).
        
    Returns:
        Tuple of (exists, is_valid, age_days, reason) where:
        - exists: True if directory exists at all
        - is_valid: True if repo appears complete and functional
        - age_days: Age in days (10_000 if unknown)
        - reason: Human-readable status explanation
    """
    # Check 1: Does the directory exist?
    if not path.exists():
        return (False, False, 10_000.0, "directory does not exist")
    
    # Check 2: Is it actually a directory?
    if not path.is_dir():
        return (True, False, 10_000.0, "path exists but is not a directory")
    
    # Check 3: Is it empty?
    try:
        contents = list(path.iterdir())
        if not contents:
            return (True, False, 10_000.0, "directory exists but is empty")
    except PermissionError as e:
        return (True, False, 10_000.0, f"cannot read directory: {e}")
    
    # Check 4: Does .git exist?
    git_dir = path / ".git"
    if not git_dir.exists():
        return (True, False, 10_000.0, "directory exists but no .git (not a git repo)")
    
    # Check 5: Does the required file exist?
    required = path / REQUIRED_FILE
    if not required.exists():
        return (True, False, 10_000.0, f"git repo exists but {REQUIRED_FILE} missing (incomplete clone?)")
    
    # Check 6: Calculate age from .git mtime
    try:
        last_modified = datetime.fromtimestamp(git_dir.stat().st_mtime)
        age_days = (timezone.now() - last_modified).total_seconds() / 86400.0
    except Exception as e:
        logger.warning(f"[GIT] Could not determine repo age: {e}")
        age_days = 10_000.0
    
    return (True, True, age_days, f"valid repo, age={age_days:.2f}d")


def is_repo_stale(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """
    Determine if repository needs to be cloned/re-cloned.
    
    Args:
        path: Path to repository root.
        max_age_days: Maximum acceptable age before re-clone.
        
    Returns:
        True if repo should be (re)cloned, False if it's fresh and valid.
    """
    exists, is_valid, age_days, reason = _check_repo_status(path)
    
    logger.debug(f"[GIT] Repo check: exists={exists}, valid={is_valid}, age={age_days:.2f}d, reason={reason}")
    
    if not exists or not is_valid:
        logger.info(f"[GIT] Repo stale: {reason}")
        return True
    
    if age_days > max_age_days:
        logger.info(f"[GIT] Repo stale: age {age_days:.2f}d exceeds max {max_age_days}d")
        return True
    
    return False


# ------------------------------------------------------------------------------
# File Locking (Unix only)
# ------------------------------------------------------------------------------

class _FileLock:
    """
    Context manager for inter-process file locking.
    
    Uses fcntl.flock() on Unix systems. On Windows (where fcntl is
    unavailable), this is a no-op — concurrent clones may race, but
    the atomic rename pattern prevents corruption.
    """
    
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fp: Optional[object] = None
    
    def __enter__(self) -> "_FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.lock_path, "w")
        
        if fcntl is not None:
            fcntl.flock(self.fp, fcntl.LOCK_EX)
            logger.debug(f"[GIT] Acquired lock: {self.lock_path}")
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.fp is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self.fp, fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                self.fp.close()
            except Exception:
                pass


# ------------------------------------------------------------------------------
# Windows-Safe Directory Operations
# ------------------------------------------------------------------------------

def _safe_remove_dir(path: Path, max_retries: int = 3, retry_delay: float = 0.5) -> bool:
    """
    Safely remove a directory, with retries for Windows file locking.
    
    Args:
        path: Directory to remove.
        max_retries: Number of retry attempts.
        retry_delay: Seconds to wait between retries.
        
    Returns:
        True if directory was removed (or didn't exist), False if removal failed.
    """
    if not path.exists():
        return True
    
    for attempt in range(max_retries):
        try:
            shutil.rmtree(path)
            return True
        except PermissionError as e:
            if attempt < max_retries - 1:
                logger.debug(f"[GIT] Removal attempt {attempt + 1} failed, retrying in {retry_delay}s: {e}")
                time.sleep(retry_delay)
            else:
                logger.warning(f"[GIT] Could not remove {path} after {max_retries} attempts: {e}")
                return False
        except Exception as e:
            logger.warning(f"[GIT] Unexpected error removing {path}: {e}")
            return False
    
    return False


def _safe_rename(src: Path, dst: Path, max_retries: int = 3, retry_delay: float = 0.5) -> bool:
    """
    Safely rename a directory, with retries for Windows file locking.
    
    If rename fails, falls back to copy + delete.
    
    Args:
        src: Source path.
        dst: Destination path.
        max_retries: Number of retry attempts.
        retry_delay: Seconds to wait between retries.
        
    Returns:
        True if rename succeeded, False otherwise.
    """
    # First, ensure destination doesn't exist
    if dst.exists():
        if not _safe_remove_dir(dst, max_retries, retry_delay):
            logger.error(f"[GIT] Cannot remove existing destination: {dst}")
            return False
    
    # Try atomic rename first
    for attempt in range(max_retries):
        try:
            src.rename(dst)
            return True
        except OSError as e:
            if attempt < max_retries - 1:
                logger.debug(f"[GIT] Rename attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(retry_delay)
            else:
                logger.debug(f"[GIT] Atomic rename failed after {max_retries} attempts: {e}")
    
    # Fallback: copy then delete
    logger.info("[GIT] Falling back to copy + delete...")
    try:
        shutil.copytree(src, dst)
        _safe_remove_dir(src)
        return True
    except Exception as e:
        logger.error(f"[GIT] Copy fallback failed: {e}")
        return False


# ------------------------------------------------------------------------------
# Clone Operations
# ------------------------------------------------------------------------------

def _do_clone(target_dir: Path, reason: str) -> bool:
    """
    Actually perform the git clone operation.
    
    Args:
        target_dir: Target directory for the repo.
        reason: Why we're cloning (for logging).
        
    Returns:
        True if clone succeeded, False otherwise.
    """
    parent_dir = target_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    
    # Use a temp directory for atomic clone
    # Put temp dir in same parent for same-filesystem rename
    tmp_name = f".imagery_utils_clone_{os.getpid()}"
    tmp_path = parent_dir / tmp_name
    
    # Clean up any leftover temp dir from previous failed attempt
    _safe_remove_dir(tmp_path)
    
    logger.info(f"[GIT] Cloning imagery_utils ({reason})...")
    logger.info(f"[GIT] Source: {REPO_URL}")
    logger.info(f"[GIT] Target: {target_dir}")
    
    # Shallow clone for speed
    cmd = ["git", "clone", "--depth", "1", REPO_URL, str(tmp_path)]
    logger.debug(f"[GIT] Command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            logger.debug(f"[GIT] stdout: {result.stdout}")
            
    except subprocess.CalledProcessError as e:
        logger.error(f"[GIT] Clone FAILED (rc={e.returncode})")
        logger.error(f"[GIT] stderr: {e.stderr}")
        _safe_remove_dir(tmp_path)
        return False
    except FileNotFoundError:
        logger.error("[GIT] Clone FAILED: 'git' command not found")
        logger.error("[GIT] Ensure git is installed and in PATH")
        return False
    
    # Verify the clone has what we need
    cloned_file = tmp_path / REQUIRED_FILE
    if not cloned_file.exists():
        logger.error(f"[GIT] Clone FAILED: {REQUIRED_FILE} not in cloned repo")
        logger.error("[GIT] This may indicate the PGC repo structure changed")
        _safe_remove_dir(tmp_path)
        return False
    
    logger.info(f"[GIT] Clone successful, {REQUIRED_FILE} verified")
    
    # Remove existing directory if present
    if target_dir.exists():
        logger.info(f"[GIT] Removing existing: {target_dir}")
        if not _safe_remove_dir(target_dir):
            logger.error("[GIT] Cannot remove existing directory")
            logger.error("[GIT] Close any programs using it (VS Code, Explorer, etc.)")
            _safe_remove_dir(tmp_path)
            return False
    
    # Move new clone into place
    logger.info(f"[GIT] Installing to: {target_dir}")
    if _safe_rename(tmp_path, target_dir):
        logger.info(f"[GIT] ✓ imagery_utils installed at {target_dir}")
        return True
    else:
        logger.error("[GIT] Failed to install cloned repo")
        _safe_remove_dir(tmp_path)
        return False


def clone_imagery_utils(external_dir: Optional[Path] = None, *, force: bool = False) -> bool:
    """
    Ensure a fresh copy of PGC imagery_utils repository is present.
    
    This function is concurrency-safe:
    - Uses inter-process file lock (.imagery_utils.lock)
    - Clones into temp directory, then atomically renames
    - Only one process performs re-clone; others skip
    
    Args:
        external_dir: Target directory for the repo. If None, uses
                      get_imagery_utils_dir() (recommended).
        force: If True, ignore staleness check and force re-clone.
        
    Returns:
        True if repo is ready to use, False if clone failed.
        
    Environment:
        GAIA_EXTERNAL_DIR: Override base external directory.
        GAIA_SKIP_REPO_UPDATE=1: Skip all update checks (for offline/CI).
        
    Example:
        >>> clone_imagery_utils()  # Uses default location
        True
        
        >>> clone_imagery_utils(force=True)  # Force re-clone
        True
    """
    # Use default location if not specified
    if external_dir is None:
        external_dir = get_imagery_utils_dir()
    else:
        external_dir = Path(external_dir).resolve()
    
    # Check for skip flag
    if os.getenv("GAIA_SKIP_REPO_UPDATE") == "1":
        logger.info("[GIT] GAIA_SKIP_REPO_UPDATE=1; skipping update check")
        _, is_valid, _, reason = _check_repo_status(external_dir)
        if is_valid:
            logger.info(f"[GIT] Existing repo valid: {reason}")
            return True
        else:
            logger.warning(f"[GIT] Repo invalid but updates disabled: {reason}")
            return False
    
    lock_file = external_dir.parent / ".imagery_utils.lock"
    
    with _FileLock(lock_file):
        # Get current status
        exists, is_valid, age_days, reason = _check_repo_status(external_dir)
        
        logger.info(f"[GIT] Status: exists={exists}, valid={is_valid}, reason={reason}")
        
        # Determine if we need to clone
        if force:
            logger.info("[GIT] Force flag set, will re-clone")
            return _do_clone(external_dir, "forced re-clone")
        
        if not exists:
            logger.info("[GIT] Directory does not exist, will clone")
            return _do_clone(external_dir, "directory missing")
        
        if not is_valid:
            logger.info(f"[GIT] Repo invalid ({reason}), will re-clone")
            return _do_clone(external_dir, f"invalid: {reason}")
        
        if age_days > MAX_AGE_DAYS:
            logger.info(f"[GIT] Repo stale (age={age_days:.2f}d > {MAX_AGE_DAYS}d), will re-clone")
            return _do_clone(external_dir, f"stale ({age_days:.1f} days old)")
        
        # Repo is valid and fresh
        logger.info(f"[GIT] ✓ imagery_utils valid and fresh (age={age_days:.2f}d)")
        return True


# ------------------------------------------------------------------------------
# Convenience Functions
# ------------------------------------------------------------------------------

def ensure_imagery_utils(*, force: bool = False) -> Optional[Path]:
    """
    Ensure imagery_utils is available and return its path.
    
    This is the recommended entry point for other modules.
    
    Args:
        force: If True, force re-clone even if repo exists.
        
    Returns:
        Path to imagery_utils directory if successful, None otherwise.
        
    Example:
        >>> from animal.utils.git_utils import ensure_imagery_utils
        >>> path = ensure_imagery_utils()
        >>> if path:
        ...     import sys
        ...     sys.path.append(str(path))
        ...     import pgc_ortho
    """
    target_dir = get_imagery_utils_dir()
    
    if clone_imagery_utils(target_dir, force=force):
        return target_dir
    return None


def get_pgc_script_path(*, force: bool = False) -> Optional[Path]:
    """
    Get path to pgc_ortho.py, cloning repo if necessary.
    
    Args:
        force: If True, force re-clone even if repo exists.
        
    Returns:
        Path to pgc_ortho.py if available, None otherwise.
    """
    target_dir = ensure_imagery_utils(force=force)
    
    if target_dir is None:
        return None
    
    script_path = target_dir / REQUIRED_FILE
    if script_path.exists():
        return script_path
    
    return None


# ------------------------------------------------------------------------------
# Microsoft Whales Repository (generate_interesting_points)
# ------------------------------------------------------------------------------

def get_whales_dir() -> Path:
    """Get directory where microsoft/whales should be cloned."""
    return get_external_dir() / "whales"


def clone_whales(
    external_dir: Optional[Path] = None, *, force: bool = False
) -> bool:
    """
    Ensure a fresh copy of microsoft/whales repository is present.

    Same concurrency-safe pattern as clone_imagery_utils.

    Args:
        external_dir: Target directory. If None, uses get_whales_dir().
        force: If True, force re-clone.

    Returns:
        True if repo is ready, False if clone failed.
    """
    if external_dir is None:
        external_dir = get_whales_dir()
    else:
        external_dir = Path(external_dir).resolve()

    return _ensure_repo(
        external_dir, WHALES_REPO_URL, WHALES_REQUIRED_FILE,
        "whales", ".whales.lock", force=force,
    )


def ensure_whales(*, force: bool = False) -> Optional[Path]:
    """
    Ensure microsoft/whales is available and return its path.

    Returns:
        Path to whales directory if successful, None otherwise.
    """
    target_dir = get_whales_dir()
    if clone_whales(target_dir, force=force):
        return target_dir
    return None


def get_whales_script_path(*, force: bool = False) -> Optional[Path]:
    """
    Get path to generate_interesting_points.py, cloning repo if needed.

    Returns:
        Path to generate_interesting_points.py if available, None otherwise.
    """
    target_dir = ensure_whales(force=force)
    if target_dir is None:
        return None
    script_path = target_dir / WHALES_REQUIRED_FILE
    if script_path.exists():
        return script_path
    return None


# ------------------------------------------------------------------------------
# Generalized Clone Helpers
# ------------------------------------------------------------------------------

def _ensure_repo(
    target_dir: Path, repo_url: str, required_file: str,
    repo_name: str, lock_name: str, *, force: bool = False,
) -> bool:
    """Shared logic for clone_imagery_utils / clone_whales."""
    if os.getenv("GAIA_SKIP_REPO_UPDATE") == "1":
        logger.info("[GIT] GAIA_SKIP_REPO_UPDATE=1; skipping %s", repo_name)
        _, is_valid, _, _ = _check_repo_status_generic(
            target_dir, required_file)
        return is_valid

    lock_file = target_dir.parent / lock_name

    with _FileLock(lock_file):
        exists, is_valid, age_days, reason = _check_repo_status_generic(
            target_dir, required_file)
        logger.info(
            "[GIT] %s status: exists=%s, valid=%s, reason=%s",
            repo_name, exists, is_valid, reason,
        )

        if force:
            reason_str = "forced re-clone"
        elif not exists:
            reason_str = "directory missing"
        elif not is_valid:
            reason_str = f"invalid: {reason}"
        elif age_days > MAX_AGE_DAYS:
            reason_str = f"stale ({age_days:.1f} days old)"
        else:
            logger.info("[GIT] %s valid and fresh (age=%.2fd)",
                        repo_name, age_days)
            return True

        return _do_clone_repo(
            target_dir, repo_url, required_file, repo_name, reason_str,
        )

def _check_repo_status_generic(
    path: Path, required_file: str
) -> Tuple[bool, bool, float, str]:
    """Check repo status with configurable required file."""
    if not path.exists():
        return (False, False, 10_000.0, "directory does not exist")
    if not path.is_dir():
        return (True, False, 10_000.0, "path exists but is not a directory")
    try:
        contents = list(path.iterdir())
        if not contents:
            return (True, False, 10_000.0, "directory exists but is empty")
    except PermissionError as e:
        return (True, False, 10_000.0, f"cannot read directory: {e}")

    git_dir = path / ".git"
    if not git_dir.exists():
        return (True, False, 10_000.0, "no .git (not a git repo)")

    required = path / required_file
    if not required.exists():
        return (True, False, 10_000.0,
                f"{required_file} missing (incomplete clone?)")

    try:
        last_modified = datetime.fromtimestamp(git_dir.stat().st_mtime)
        age_days = (timezone.now() - last_modified).total_seconds() / 86400.0
    except Exception:
        age_days = 10_000.0

    return (True, True, age_days, f"valid repo, age={age_days:.2f}d")


def _do_clone_repo(
    target_dir: Path, repo_url: str, required_file: str,
    repo_name: str, reason: str,
) -> bool:
    """Clone a git repo with atomic rename pattern."""
    parent_dir = target_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    tmp_name = f".{repo_name}_clone_{os.getpid()}"
    tmp_path = parent_dir / tmp_name
    _safe_remove_dir(tmp_path)

    logger.info("[GIT] Cloning %s (%s)...", repo_name, reason)
    logger.info("[GIT] Source: %s", repo_url)
    logger.info("[GIT] Target: %s", target_dir)

    cmd = ["git", "clone", "--depth", "1", repo_url, str(tmp_path)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error("[GIT] Clone FAILED (rc=%d)", e.returncode)
        logger.error("[GIT] stderr: %s", e.stderr)
        _safe_remove_dir(tmp_path)
        return False
    except FileNotFoundError:
        logger.error("[GIT] Clone FAILED: 'git' command not found")
        return False

    if not (tmp_path / required_file).exists():
        logger.error("[GIT] Clone FAILED: %s not in cloned repo", required_file)
        _safe_remove_dir(tmp_path)
        return False

    logger.info("[GIT] Clone successful, %s verified", required_file)

    if target_dir.exists():
        if not _safe_remove_dir(target_dir):
            _safe_remove_dir(tmp_path)
            return False

    if _safe_rename(tmp_path, target_dir):
        logger.info("[GIT] %s installed at %s", repo_name, target_dir)
        return True
    else:
        _safe_remove_dir(tmp_path)
        return False


# ------------------------------------------------------------------------------
# Module Entry Point (for testing)
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    """
    CLI entry point for testing.
    
    Usage:
        python git_utils.py [--force] [--status] [--verbose]
    """
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Clone/update PGC imagery_utils repository"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force re-clone regardless of age"
    )
    parser.add_argument(
        "--status", "-s",
        action="store_true",
        help="Just check status, don't clone"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    
    args = parser.parse_args()
    
    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Get target directory
    target_dir = get_imagery_utils_dir()
    
    print(f"External base: {get_external_dir()}")
    print(f"Target: {target_dir}")
    
    if args.status:
        exists, is_valid, age_days, reason = _check_repo_status(target_dir)
        print(f"Exists: {exists}")
        print(f"Valid: {is_valid}")
        print(f"Age: {age_days:.2f} days")
        print(f"Reason: {reason}")
        sys.exit(0 if is_valid else 1)
    
    print(f"Force: {args.force}")
    
    success = clone_imagery_utils(target_dir, force=args.force)
    
    if success:
        print("✓ Repository ready")
        
        # Verify PGC tools are importable
        sys.path.append(str(target_dir))
        try:
            import pgc_ortho  # noqa: F401
            print("✓ pgc_ortho imported successfully")
        except ImportError as e:
            print(f"✗ pgc_ortho import failed: {e}")
            sys.exit(1)
    else:
        print("✗ Repository not ready")
        sys.exit(1)