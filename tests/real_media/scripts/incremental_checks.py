"""Assertion helpers for the BVT incremental-indexing phases (M-8/S-1, S-3).

Shared by the dev-env harness (run-local.sh), the Linux/macOS bundle
validators (via incremental-phases.sh), and the Windows bundle validator
(validate-installed-bundle-windows.ps1). Stdlib-only — EXCEPT the two
qdrant-* subcommands (M-8/S-3), which lazily import qdrant-client and must
run on the app/workspace venv python (the same interpreter the phases
already pass via --python). Prints PASS details to stdout; exits non-zero
with a diagnostic on the first failed check.

Subcommands:
  snapshot        --sqlite DB --out FILE
  compare-counts  --sqlite DB --baseline FILE [--tables t1,t2,...]
  assert-complete --log FILE [--log FILE ...] [--expect k=v ...]
                  [--expect-gt k=v ...] [--expect-eq-key k1=k2 ...]
  assert-contains --log FILE [--log FILE ...] --text TEXT
  assert-media-state --sqlite DB [--moved-name NAME --moved-rel REL]
                     [--deleted-name NAME]
  assert-export-sent --log FILE [--log FILE ...] [--expect-image N]
                     [--expect-video N] [--min-deleted N]
      (M-8/S-3) Parse the exporter's sent/deleted counts from a phase log
      and assert they are single-change-sized, not library-sized.
  qdrant-count    --qdrant DIR --collection NAME --out FILE
      (M-8/S-3) Snapshot an embedded-Qdrant collection's point count.
      Opens embedded Qdrant — only safe while no API/indexer holds it
      (the phases run before the API starts).
  assert-qdrant-drop --qdrant DIR --collection NAME --baseline FILE
                     --sqlite DB --deleted-name NAME
      (M-8/S-3) Assert the collection's point count dropped by EXACTLY the
      tombstoned media's keyframe count since the baseline snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# Content tables: rows here represent indexed content. scan_run is excluded
# on purpose — it legitimately grows one row per indexer run.
CONTENT_TABLES = [
    "media",
    "image_embedding",
    "keyframe_embedding",
    "face_embedding",
    "face",
    "video_keyframes",
    "shots",
    "tag",
    "media_tag",
]

_SUMMARY_RE = re.compile(r"INDEXER_SUMMARY (?P<payload>\{.*\})")


def _fail(message: str) -> None:
    print(f"INCREMENTAL-CHECK FAIL: {message}")
    sys.exit(1)


def _read_logs(paths: list[str]) -> str:
    chunks = []
    for raw in paths:
        p = Path(raw)
        if p.exists():
            chunks.append(p.read_text(errors="replace"))
    if not chunks:
        _fail(f"none of the log files exist: {paths}")
    return "\n".join(chunks)


def _last_complete_payload(text: str) -> dict:
    payloads = []
    for match in _SUMMARY_RE.finditer(text):
        try:
            data = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("phase") == "complete":
            payloads.append(data)
    if not payloads:
        _fail("no INDEXER_SUMMARY phase=complete line found in the log(s)")
    return payloads[-1]


def _table_counts(sqlite_path: str, tables: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(sqlite_path)
    try:
        counts = {}
        for table in tables:
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = -1  # table absent
        return counts
    finally:
        conn.close()


def cmd_snapshot(args) -> None:
    counts = _table_counts(args.sqlite, CONTENT_TABLES)
    Path(args.out).write_text(json.dumps(counts, indent=2, sort_keys=True))
    print(f"snapshot written: {args.out} -> {counts}")


def cmd_compare_counts(args) -> None:
    baseline = json.loads(Path(args.baseline).read_text())
    tables = args.tables.split(",") if args.tables else list(baseline.keys())
    current = _table_counts(args.sqlite, tables)
    diffs = {
        t: (baseline.get(t), current.get(t))
        for t in tables
        if baseline.get(t) != current.get(t)
    }
    if diffs:
        _fail(f"content-table row counts changed: {diffs}")
    print(f"content-table row counts unchanged for: {', '.join(tables)}")


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            _fail(f"bad key=value argument: {pair}")
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def cmd_assert_complete(args) -> None:
    text = _read_logs(args.log)
    payload = _last_complete_payload(text)
    for key, expected in _parse_kv(args.expect).items():
        actual = payload.get(key)
        if str(actual) != expected:
            _fail(
                f"complete payload {key}={actual!r}, expected {expected!r}. "
                f"Payload: {payload}"
            )
    for key, floor in _parse_kv(args.expect_gt).items():
        actual = payload.get(key)
        if actual is None or not float(actual) > float(floor):
            _fail(
                f"complete payload {key}={actual!r}, expected > {floor}. "
                f"Payload: {payload}"
            )
    for pair, other in _parse_kv(args.expect_eq_key).items():
        left, right = payload.get(pair), payload.get(other)
        if left != right:
            _fail(
                f"complete payload {pair}={left!r} != {other}={right!r}. "
                f"Payload: {payload}"
            )
    print(f"complete payload OK: {payload}")


def cmd_assert_contains(args) -> None:
    text = _read_logs(args.log)
    if args.text not in text:
        _fail(f"expected log text not found: {args.text!r}")
    print(f"log contains: {args.text!r}")


def cmd_assert_media_state(args) -> None:
    conn = sqlite3.connect(args.sqlite)
    try:
        if args.moved_name:
            row = conn.execute(
                "SELECT rel_path, deleted FROM media WHERE rel_path LIKE ? AND deleted = 0",
                (f"%{args.moved_name}",),
            ).fetchone()
            if row is None:
                _fail(f"no live media row found for moved file {args.moved_name}")
            if row[0] != args.moved_rel:
                _fail(
                    f"moved file rel_path is {row[0]!r}, expected {args.moved_rel!r}"
                )
            print(f"moved file OK: rel_path={row[0]}")
        if args.deleted_name:
            rows = conn.execute(
                "SELECT media_id, deleted FROM media WHERE rel_path LIKE ?",
                (f"%{args.deleted_name}",),
            ).fetchall()
            if not rows:
                _fail(f"no media row at all for deleted file {args.deleted_name}")
            live = [r for r in rows if not r[1]]
            if live:
                _fail(
                    f"deleted file {args.deleted_name} still has live media rows: {live}"
                )
            print(f"deleted file OK: {len(rows)} row(s), all tombstoned")
    finally:
        conn.close()


# M-8/S-3: the single unambiguous per-run summary lines the pipeline emits.
# A run that sent points logs the "complete (image=N video=N)" line; a
# legitimate 0-dirty delta run (deletion-only or no-op) logs the no-op line
# instead — both count as an export summary, the no-op as image=0 video=0.
_EXPORT_SENT_RE = re.compile(
    r"Qdrant image/video export complete \(image=(?P<image>\d+) video=(?P<video>\d+)\)"
)
_EXPORT_NOOP_RE = re.compile(
    r"Delta export: no dirty image/video rows to send \(0-dirty no-op\)"
)
_DELETED_POINTS_RE = re.compile(
    r"Tombstone deletion pass: (?P<deleted>\d+) point\(s\) deleted"
)


def cmd_assert_export_sent(args) -> None:
    """S-3: the export log's sent counts must be single-change-sized."""
    text = _read_logs(args.log)
    sent = [(m.start(), m.groupdict()) for m in _EXPORT_SENT_RE.finditer(text)]
    sent += [
        (m.start(), {"image": "0", "video": "0"})
        for m in _EXPORT_NOOP_RE.finditer(text)
    ]
    sent.sort(key=lambda pair: pair[0])
    if args.expect_image is not None or args.expect_video is not None:
        if not sent:
            _fail(
                "no 'Qdrant image/video export complete' or 0-dirty no-op "
                "line found in the log(s)"
            )
        last = sent[-1][1]
        if args.expect_image is not None and int(last["image"]) != args.expect_image:
            _fail(
                f"export sent image={last['image']}, expected {args.expect_image} "
                "(delta export must be single-change-sized, not library-sized)"
            )
        if args.expect_video is not None and int(last["video"]) != args.expect_video:
            _fail(
                f"export sent video={last['video']}, expected {args.expect_video}"
            )
        print(f"export sent OK: image={last['image']} video={last['video']}")
    if args.min_deleted is not None:
        deleted = [int(m.group("deleted")) for m in _DELETED_POINTS_RE.finditer(text)]
        if not deleted:
            _fail("no 'Tombstone deletion pass' line found in the log(s)")
        if deleted[-1] < args.min_deleted:
            _fail(
                f"deletion pass removed {deleted[-1]} point(s), expected >= {args.min_deleted}"
            )
        print(f"deletion pass OK: {deleted[-1]} point(s) deleted")


def _qdrant_collection_count(qdrant_dir: str, collection: str) -> int:
    """Embedded-Qdrant point count. Lazy import — see module docstring."""
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        _fail(
            "qdrant-client not importable — run the qdrant-* subcommands with "
            "the app/workspace venv python (the --python the phases receive)"
        )
    client = QdrantClient(path=qdrant_dir)
    try:
        try:
            return int(client.count(collection).count)
        except Exception:
            return 0  # collection absent
    finally:
        try:
            client.close()
        except Exception:
            pass


def cmd_qdrant_count(args) -> None:
    count = _qdrant_collection_count(args.qdrant, args.collection)
    Path(args.out).write_text(
        json.dumps({"collection": args.collection, "count": count}, sort_keys=True)
    )
    print(f"qdrant snapshot written: {args.out} -> {args.collection}={count}")


def cmd_assert_qdrant_drop(args) -> None:
    """S-3: the deleted video's points are gone — count dropped by exactly
    its (soft-delete-surviving) keyframe row count."""
    baseline = json.loads(Path(args.baseline).read_text())
    if baseline.get("collection") != args.collection:
        _fail(
            f"baseline is for collection {baseline.get('collection')!r}, "
            f"not {args.collection!r}"
        )
    before = int(baseline["count"])
    after = _qdrant_collection_count(args.qdrant, args.collection)

    conn = sqlite3.connect(args.sqlite)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM video_keyframes WHERE video_id IN (
                SELECT media_id FROM media
                WHERE rel_path LIKE ? AND deleted = 1
            )
            """,
            (f"%{args.deleted_name}",),
        ).fetchone()
        expected_drop = int(row[0])
    finally:
        conn.close()

    if expected_drop <= 0:
        _fail(
            f"tombstoned video {args.deleted_name} has no surviving keyframe "
            "rows — cannot verify the point-count drop"
        )
    if before - after != expected_drop:
        _fail(
            f"{args.collection} point count went {before} -> {after} "
            f"(drop {before - after}), expected a drop of exactly "
            f"{expected_drop} (the deleted video's keyframe count)"
        )
    print(
        f"qdrant drop OK: {args.collection} {before} -> {after} "
        f"(-{expected_drop}, the deleted video's keyframes)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot")
    p.add_argument("--sqlite", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("compare-counts")
    p.add_argument("--sqlite", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--tables", default=None)
    p.set_defaults(func=cmd_compare_counts)

    p = sub.add_parser("assert-complete")
    p.add_argument("--log", action="append", required=True)
    p.add_argument("--expect", action="append", default=[])
    p.add_argument("--expect-gt", action="append", default=[])
    p.add_argument("--expect-eq-key", action="append", default=[])
    p.set_defaults(func=cmd_assert_complete)

    p = sub.add_parser("assert-contains")
    p.add_argument("--log", action="append", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_assert_contains)

    p = sub.add_parser("assert-media-state")
    p.add_argument("--sqlite", required=True)
    p.add_argument("--moved-name", default=None)
    p.add_argument("--moved-rel", default=None)
    p.add_argument("--deleted-name", default=None)
    p.set_defaults(func=cmd_assert_media_state)

    p = sub.add_parser("assert-export-sent")
    p.add_argument("--log", action="append", required=True)
    p.add_argument("--expect-image", type=int, default=None)
    p.add_argument("--expect-video", type=int, default=None)
    p.add_argument("--min-deleted", type=int, default=None)
    p.set_defaults(func=cmd_assert_export_sent)

    p = sub.add_parser("qdrant-count")
    p.add_argument("--qdrant", required=True)
    p.add_argument("--collection", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_qdrant_count)

    p = sub.add_parser("assert-qdrant-drop")
    p.add_argument("--qdrant", required=True)
    p.add_argument("--collection", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--sqlite", required=True)
    p.add_argument("--deleted-name", required=True)
    p.set_defaults(func=cmd_assert_qdrant_drop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
