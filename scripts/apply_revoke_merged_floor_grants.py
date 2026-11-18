"""Take the merged-away A185 floors off every user's floor access.

Asked for on 18 Nov 2026, after the floor merge (scripts/apply_a185_floor_merge.py)
left FFS Packing Area, Dmart Production Area and Sorting Area at 0:

    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_revoke_merged_floor_grants.py            # plan only, reads
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_revoke_merged_floor_grants.py --dry-run  # writes, checks, rolls back
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_revoke_merged_floor_grants.py --apply    # writes, checks, commits

What it changes: auth_user.allowed_floors, removing those three names (any case
or spacing) and keeping every other floor in its order. Role grants
(auth_role_permission) are checked too and changed the same way if any name
them. Nothing else on the profile moves.

AN EMPTY LIST MEANS "EVERY FLOOR" (auth_schema.sql:35), so a user whose only
floors were these three would be WIDENED to the whole building by removing them.
The script refuses that user and names them instead of writing.

Grants are read from auth_user on every request (auth_service.validate_session),
so the change applies at once; nobody has to log in again.

--apply first saves each changed user's previous floors to
ERP/backups/floor_grants_before_revoke_<timestamp>.csv, the record to restore from.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import asyncpg

BACKEND = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(BACKEND / "scripts")]
from backup_stock_take_tables import rds_dsn  # noqa: E402

REVOKE = ("FFS Packing Area", "Dmart Production Area", "Sorting Area")
KEYS = {f.strip().upper() for f in REVOKE}
BACKUPS = BACKEND.parent / "backups"


class _Rollback(Exception):
    pass


def strip(floors) -> list[str]:
    return [f for f in (floors or []) if str(f).strip().upper() not in KEYS]


async def plan(conn) -> tuple[list[dict], list[dict], list[dict]]:
    users = [dict(r) for r in await conn.fetch(
        """SELECT user_id, COALESCE(NULLIF(BTRIM(full_name), ''), email, phone) AS who,
                  is_active, allowed_floors
             FROM auth_user
            WHERE EXISTS (SELECT 1 FROM unnest(allowed_floors) f WHERE UPPER(BTRIM(f)) = ANY($1))
            ORDER BY user_id""", list(KEYS))]
    roles = [dict(r) for r in await conn.fetch(
        """SELECT role_id, permission_id, allowed_floors FROM auth_role_permission
            WHERE EXISTS (SELECT 1 FROM unnest(allowed_floors) f WHERE UPPER(BTRIM(f)) = ANY($1))""",
        list(KEYS))]
    ok, refused = [], []
    for u in users:
        u["new_floors"] = strip(u["allowed_floors"])
        u["removed"] = [f for f in u["allowed_floors"] if f not in u["new_floors"]]
        (ok if u["new_floors"] else refused).append(u)
    # A role grant left with no floors has no floor restriction either
    # (permission_service skips the check when allowed_floors is empty or NULL),
    # so it is refused the same way a user is, never written.
    for r in roles:
        r["new_floors"] = strip(r["allowed_floors"])
        if not r["new_floors"]:
            refused.append({"user_id": f"role {r['role_id']}/perm {r['permission_id']}",
                            "who": "role grant", "allowed_floors": r["allowed_floors"]})
    roles = [r for r in roles if r["new_floors"]]
    return ok, refused, roles


def report(ok, refused, roles) -> None:
    print(f"{len(ok)} user(s) lose the merged floors:")
    for u in ok:
        print(f"   {u['user_id']:>4} {u['who'][:26]:<26} {'active ' if u['is_active'] else 'INACTIVE'} "
              f"- {', '.join(u['removed'])}  ({len(u['allowed_floors'])} -> {len(u['new_floors'])} floors)")
    if refused:
        print(f"\nREFUSED - {len(refused)} user(s) / role grant(s) hold ONLY merged floors; "
              f"an empty list would mean every floor:")
        for u in refused:
            print(f"   {u['user_id']:>4} {u['who']}: {u['allowed_floors']}")
    print(f"\nrole grants naming them: {len(roles)}")
    # Who loses a floor without holding the floor it went into -- they can no
    # longer post that stock anywhere until an admin adds it.
    into = {"FFS PACKING AREA": "FG store", "DMART PRODUCTION AREA": "Dmart Packing Area",
            "SORTING AREA": "Dmart Packing Area"}
    gaps = []
    for u in ok:
        held = {f.strip().upper() for f in u["new_floors"]}
        need = sorted({into[f.strip().upper()] for f in u["removed"]} - {t for t in into.values()
                                                                          if t.upper() in held})
        if need:
            gaps.append(f"{u['who']} ({'active' if u['is_active'] else 'inactive'}): no {', '.join(need)}")
    if gaps:
        print("\nheld a merged floor but not the floor it went into (not added - an admin's call):")
        for g in gaps:
            print("  ", g)


async def main(a) -> None:
    dsn = rds_dsn()
    print("database:", dsn.split("@", 1)[-1].split("/", 1)[0])
    conn = await asyncpg.connect(dsn, timeout=30)
    try:
        if not (a.apply or a.dry_run):
            async with conn.transaction(readonly=True):
                ok, refused, roles = await plan(conn)
            report(ok, refused, roles)
            print("\nPLAN ONLY - nothing written.")
            return
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("SET LOCAL lock_timeout = '10s'")
            ok, refused, roles = await plan(conn)
            report(ok, refused, roles)
            if not ok and not roles:
                print("\nNothing to revoke.")
                raise _Rollback
            if a.apply:
                BACKUPS.mkdir(exist_ok=True)
                bk = BACKUPS / f"floor_grants_before_revoke_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
                with bk.open("w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["kind", "user_id_or_role_id", "permission_id", "who", "allowed_floors_before_json"])
                    for u in ok:
                        w.writerow(["user", u["user_id"], "", u["who"], json.dumps(u["allowed_floors"])])
                    for r in roles:
                        w.writerow(["role", r["role_id"], r["permission_id"], "", json.dumps(r["allowed_floors"])])
                print(f"\nprevious grants saved: {bk}")
            for u in ok:
                await conn.execute("UPDATE auth_user SET allowed_floors = $1 WHERE user_id = $2",
                                   u["new_floors"], u["user_id"])
            for r in roles:
                await conn.execute(
                    "UPDATE auth_role_permission SET allowed_floors = $1 WHERE role_id = $2 AND permission_id = $3",
                    r["new_floors"], r["role_id"], r["permission_id"])
            left_users, _, left_roles = await plan(conn)
            problems = [f"user {u['user_id']} still holds {u['removed']}" for u in left_users
                        if u["user_id"] in {x["user_id"] for x in ok}] + \
                       [f"role {r['role_id']}/{r['permission_id']} still names one" for r in left_roles]
            for u in ok:
                now = await conn.fetchval("SELECT allowed_floors FROM auth_user WHERE user_id = $1", u["user_id"])
                if list(now or []) != u["new_floors"]:
                    problems.append(f"user {u['user_id']}: floors read back as {now}")
                if not now:
                    problems.append(f"user {u['user_id']}: would be left with no floors (= every floor)")
            if problems:
                print("\nCHECK FAILED - rolled back:")
                for p in problems:
                    print("  ", p)
                raise _Rollback
            print(f"\nchecks passed: {len(ok)} user(s) no longer hold the merged floors; every other floor kept.")
            if a.dry_run:
                print("DRY RUN - rolled back, nothing written.")
                raise _Rollback
            await tr.commit()
            print("COMMITTED.")
        except _Rollback:
            await tr.rollback()
        except BaseException:
            await tr.rollback()
            raise
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="write, check and roll back")
    g.add_argument("--apply", action="store_true", help="write, check and commit")
    asyncio.run(main(ap.parse_args()))
