"""Delete old threads from Nova's checkpoint database and reclaim the space.

    python scripts/prune_checkpoints.py                  # dry run: what would go
    python scripts/prune_checkpoints.py --older-than 14  # dry run, 14-day cutoff
    python scripts/prune_checkpoints.py --older-than 14 --apply

Whole threads only. Keeping just the newest checkpoint of a thread looks like the
obvious saving, but LangGraph's ``DeltaChannel`` stores most steps as deltas that
are rebuilt by walking the parent chain, so dropping the ancestors makes the
survivor reconstruct as an empty conversation, with no error. A thread is either
kept intact or removed.

A thread's age is the time of its newest checkpoint, read from the checkpoint id
(a time-ordered UUIDv6), so nothing has to be deserialised.

``--apply`` needs Nova closed: VACUUM takes an exclusive lock, and deleting under
a live session would pull state out from under it. The script checks and refuses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys
import uuid

DB = pathlib.Path.home() / ".nova" / "checkpoints" / "nova_checkpoints.db"
_GREGORIAN = dt.datetime(1582, 10, 15, tzinfo=dt.timezone.utc)


def checkpoint_time(checkpoint_id: str) -> dt.datetime | None:
    """When a UUIDv6 checkpoint id was minted, or ``None`` if it is not one."""
    try:
        u = uuid.UUID(checkpoint_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if u.version != 6:
        return None
    h = u.hex  # time_high(8) time_mid(4) version(1) time_low(3)
    ticks = int(h[:12] + h[13:16], 16)  # 100 ns since 1582-10-15
    return _GREGORIAN + dt.timedelta(microseconds=ticks // 10)


def survey(con: sqlite3.Connection, cutoff: dt.datetime) -> tuple[list[str], dict]:
    """Thread ids last touched before *cutoff*, and totals for the report."""
    rows = con.execute(
        "select thread_id, count(*), max(checkpoint_id), sum(length(checkpoint)) "
        "from checkpoints group by thread_id"
    ).fetchall()
    old: list[str] = []
    stats = {"threads": len(rows), "old_threads": 0, "bytes": 0, "old_bytes": 0, "undated": 0}
    for thread_id, _n, newest, size in rows:
        size = size or 0
        stats["bytes"] += size
        when = checkpoint_time(newest)
        if when is None:
            stats["undated"] += 1  # never deleted: its age is unknown
            continue
        if when < cutoff:
            old.append(thread_id)
            stats["old_threads"] += 1
            stats["old_bytes"] += size
    return old, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--older-than", type=int, default=14, metavar="DAYS")
    ap.add_argument("--apply", action="store_true", help="delete and VACUUM (default: dry run)")
    ap.add_argument("--db", type=pathlib.Path, default=DB)
    args = ap.parse_args()

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than)
    mode = "rw" if args.apply else "ro"
    con = sqlite3.connect(f"file:{args.db.as_posix()}?mode={mode}", uri=True, timeout=5)
    old, s = survey(con, cutoff)
    gb = 1 / 1024**3
    print(f"{args.db}  {args.db.stat().st_size * gb:.1f} GB")
    print(f"threads: {s['threads']}  checkpoint payload: {s['bytes'] * gb:.1f} GB")
    print(
        f"older than {args.older_than} days: {s['old_threads']} threads, "
        f"{s['old_bytes'] * gb:.1f} GB of payload ({s['undated']} undated threads are kept)"
    )
    if not args.apply:
        print("dry run: nothing deleted. Re-run with --apply, with Nova closed.")
        return 0

    try:
        con.execute("begin exclusive")
    except sqlite3.OperationalError:
        print("the database is in use: close every Nova session and retry", file=sys.stderr)
        return 1
    for i in range(0, len(old), 500):
        chunk = old[i : i + 500]
        marks = ",".join("?" * len(chunk))
        con.execute(f"delete from writes where thread_id in ({marks})", chunk)
        con.execute(f"delete from checkpoints where thread_id in ({marks})", chunk)
    con.commit()
    print(f"deleted {len(old)} threads; reclaiming space (this rewrites the file)...")
    con.execute("vacuum")
    con.close()
    print(f"done: {args.db.stat().st_size * gb:.1f} GB")
    return 0


def demo() -> None:
    """Self-check: id dating, and that only threads past the cutoff are deleted."""
    assert checkpoint_time("1f1bfef3-fbca-6ef6-80f6-320b7c3bd95a").year == 2026  # type: ignore[union-attr]
    assert checkpoint_time("not-a-uuid") is None
    con = sqlite3.connect(":memory:")
    con.execute("create table checkpoints (thread_id, checkpoint_id, checkpoint)")
    con.executemany(
        "insert into checkpoints values (?,?,?)",
        [("new", "1f1bfef3-fbca-6ef6-80f6-320b7c3bd95a", b"x" * 10),
         ("old", "1f0b67c9-02c6-6211-8e3f-2ce3d1b50cfd", b"x" * 30),
         ("odd", "not-a-uuid", b"x")],
    )
    cutoff = checkpoint_time("1f1bfef3-fbca-6ef6-80f6-320b7c3bd95a") - dt.timedelta(days=14)  # type: ignore[operator]
    old, s = survey(con, cutoff)
    assert old == ["old"] and s["old_bytes"] == 30 and s["undated"] == 1, (old, s)
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        demo()
    else:
        raise SystemExit(main())
