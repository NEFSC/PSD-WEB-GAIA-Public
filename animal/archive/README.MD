# Archive

Superseded modules with known limitations. Retained for rollback capability and regression testing.

**Why keep these?** Same reason we keep raw imagery before processing — operational continuity. If a rewrite introduces unexpected behavior in production, we can diff against the original to isolate the change. Deleting them saves ~50KB but costs us forensic capability.

**Why not fix them in place?** Some issues (hardcoded paths, missing platform support) required structural changes incompatible with surgical patches. A rewrite with the same public API is lower-risk than incremental fixes that may miss edge cases.

**When to delete?** After the replacement has run successfully in production for one full processing cycle (search → COG → upload). At that point, git history is sufficient.

---

| File | Superseded | Known Limitation | Replacement |
|------|------------|------------------|-------------|
| pgc_wrapper_v1.py | 2026-01-20 | `_build_clean_env()` assumes Linux conda paths; fails on Windows with `Cannot find proj.db` | `animal/utils/pgc_wrapper.py` |

---

*This is not dead code. It is quarantined code awaiting decommission confirmation.*