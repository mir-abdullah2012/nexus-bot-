"""One-time importer: Nexus 1.x JSON files -> Nexus 2.0 SQLite.

Standard library only, on purpose -- it must run anywhere the JSON files landed,
including a machine with no bot dependencies installed.

    python scripts/migrate_json.py --dry-run          # preview, writes nothing
    python scripts/migrate_json.py                    # real import (backs up first)

Safety properties:
  * --dry-run does everything against an in-memory database and touches no files.
  * A real run copies every JSON file into backups/<timestamp>/ before writing.
  * The JSON files themselves are never modified or deleted.
  * If the target database already holds players, the run aborts unless --force,
    so you cannot clobber live data by re-running this by accident.
  * Within a run every write is an upsert or a de-duplicated insert, so the same
    import applied twice can never double a balance or a warning.
"""

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import migrations  # noqa: E402

JSON_FILES = ("economy.json", "warnings.json", "config.json", "reminders.json")


# ============================================================
#  LOADING
# ============================================================
def load_json(path: Path):
    """Tolerant load, matching the Nexus 1.x _load(): never raise, never crash."""
    if not path.exists():
        return {}, "MISSING"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), "found"
    except Exception as e:
        return {}, f"UNREADABLE ({e})"


def as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================
#  IMPORT STEPS
# ============================================================
def import_players(conn, economy, report):
    now = int(time.time())
    for raw_uid, data in economy.items():
        uid = as_int(raw_uid, None)
        if uid is None:
            report["skipped"].append(f"economy.json: bad user id {raw_uid!r}")
            continue
        if not isinstance(data, dict):
            report["skipped"].append(f"economy.json: bad record for {raw_uid!r}")
            continue

        conn.execute(
            "INSERT INTO players (user_id, balance, xp, level, last_daily, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  balance = excluded.balance, xp = excluded.xp, level = excluded.level, "
            "  last_daily = excluded.last_daily, updated_at = excluded.updated_at",
            (
                uid,
                as_int(data.get("bal"), 0),
                as_int(data.get("xp"), 0),
                as_int(data.get("level"), 1),
                as_int(data.get("last_daily"), 0),
                now,
                now,
            ),
        )
        report["players"] += 1

        for item in data.get("inventory") or []:
            conn.execute(
                "INSERT INTO inventory (user_id, item_code, quantity, acquired_at) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(user_id, item_code) DO NOTHING",
                (uid, str(item), now),
            )
            report["inventory"] += 1


def import_warnings(conn, warnings, report):
    for key, entries in warnings.items():
        if ":" not in str(key):
            report["skipped"].append(f"warnings.json: bad key {key!r}")
            continue
        raw_guild, _, raw_user = str(key).partition(":")
        guild_id = as_int(raw_guild, None)
        user_id = as_int(raw_user, None)
        if guild_id is None or user_id is None:
            report["skipped"].append(f"warnings.json: bad key {key!r}")
            continue

        for entry in entries or []:
            reason = str(entry.get("reason", "No reason given"))
            created = as_int(entry.get("time"), 0)
            # De-duplicate so a re-run cannot multiply someone's warning count.
            existing = conn.execute(
                "SELECT 1 FROM warnings WHERE guild_id = ? AND user_id = ? "
                "AND reason = ? AND created_at = ?",
                (guild_id, user_id, reason, created),
            ).fetchone()
            if existing:
                continue
            # moderator_id stays NULL: the old format never recorded who warned.
            conn.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at, active) "
                "VALUES (?, ?, NULL, ?, ?, 1)",
                (guild_id, user_id, reason, created),
            )
            report["warnings"] += 1


def import_config(conn, config, report):
    now = int(time.time())
    for raw_gid, cfg in config.items():
        guild_id = as_int(raw_gid, None)
        if guild_id is None or not isinstance(cfg, dict):
            report["skipped"].append(f"config.json: bad entry {raw_gid!r}")
            continue

        conn.execute(
            "INSERT INTO guild_config "
            "(guild_id, welcome_channel, log_channel, ai_enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "  welcome_channel = excluded.welcome_channel, "
            "  log_channel = excluded.log_channel, "
            "  ai_enabled = excluded.ai_enabled, updated_at = excluded.updated_at",
            (
                guild_id,
                as_int(cfg.get("welcome_channel"), None) if cfg.get("welcome_channel") else None,
                as_int(cfg.get("log_channel"), None) if cfg.get("log_channel") else None,
                1 if cfg.get("ai_enabled", True) else 0,
                now,
                now,
            ),
        )
        report["guilds"] += 1

        for role_name in cfg.get("self_roles") or []:
            conn.execute(
                "INSERT OR IGNORE INTO guild_self_roles (guild_id, role_name) VALUES (?, ?)",
                (guild_id, str(role_name)),
            )
            report["self_roles"] += 1

        for word in cfg.get("extra_banned") or []:
            conn.execute(
                "INSERT OR IGNORE INTO guild_banned_words (guild_id, word, added_by, added_at) "
                "VALUES (?, ?, NULL, ?)",
                (guild_id, str(word).lower(), now),
            )
            report["banned_words"] += 1


def import_reminders(conn, reminders, report):
    now = int(time.time())
    items = reminders.get("list", []) if isinstance(reminders, dict) else reminders
    for entry in items or []:
        if not isinstance(entry, dict):
            report["skipped"].append(f"reminders.json: bad entry {entry!r}")
            continue
        user_id = as_int(entry.get("user_id"), None)
        channel_id = as_int(entry.get("channel_id"), None)
        if user_id is None or channel_id is None:
            report["skipped"].append(f"reminders.json: bad entry {entry!r}")
            continue

        remind_at = as_int(entry.get("remind_at"), 0)
        text = str(entry.get("text", ""))
        existing = conn.execute(
            "SELECT 1 FROM reminders WHERE user_id = ? AND channel_id = ? "
            "AND remind_at = ? AND text = ?",
            (user_id, channel_id, remind_at, text),
        ).fetchone()
        if existing:
            continue
        # guild_id stays NULL: the old format never recorded it.
        conn.execute(
            "INSERT INTO reminders "
            "(user_id, channel_id, guild_id, remind_at, text, created_at, fired) "
            "VALUES (?, ?, NULL, ?, ?, ?, 0)",
            (user_id, channel_id, remind_at, text, now),
        )
        report["reminders"] += 1


# ============================================================
#  MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Import Nexus 1.x JSON data into SQLite.")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT,
                        help="folder holding the .json files (default: project root)")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "nexus.db",
                        help="target SQLite file (default: nexus.db in project root)")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview only: runs against an in-memory DB, writes nothing")
    parser.add_argument("--force", action="store_true",
                        help="import even if the target database already has players")
    parser.add_argument("--backup-dir", type=Path, default=PROJECT_ROOT / "backups",
                        help="where to put the pre-import backup (default: ./backups)")
    args = parser.parse_args()

    mode = "DRY RUN (nothing will be written)" if args.dry_run else "LIVE IMPORT"
    print()
    print("=" * 62)
    print("  NEXUS 2.0  --  JSON to SQLite migration")
    print("=" * 62)
    print(f"  mode:   {mode}")
    print(f"  source: {args.source}")
    print(f"  target: {args.db if not args.dry_run else ':memory:'}")
    print()

    # ---------- read source ----------
    print("SOURCE FILES")
    print("-" * 62)
    payloads = {}
    for name in JSON_FILES:
        path = args.source / name
        data, status = load_json(path)
        payloads[name] = data
        size = f"{path.stat().st_size} bytes" if path.exists() else "-"
        note = "" if status == "found" else "   (treated as empty)"
        print(f"  {name:<16} {status:<12} {size}{note}")
    print()

    economy = payloads["economy.json"]
    warnings = payloads["warnings.json"]
    config = payloads["config.json"]
    reminders = payloads["reminders.json"]

    # ---------- expected totals, straight from the JSON ----------
    src_players = len(economy)
    src_balance = sum(as_int(v.get("bal"), 0) for v in economy.values() if isinstance(v, dict))
    src_xp = sum(as_int(v.get("xp"), 0) for v in economy.values() if isinstance(v, dict))
    src_inventory = sum(len(v.get("inventory") or []) for v in economy.values()
                        if isinstance(v, dict))
    src_warnings = sum(len(v or []) for v in warnings.values())
    src_guilds = len(config)
    src_reminders = len(reminders.get("list", []) if isinstance(reminders, dict) else reminders or [])

    # ---------- guard against clobbering live data ----------
    if not args.dry_run and args.db.exists():
        probe = sqlite3.connect(args.db)
        try:
            has_players_table = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='players'"
            ).fetchone()
            existing = probe.execute("SELECT COUNT(*) FROM players").fetchone()[0] \
                if has_players_table else 0
        finally:
            probe.close()
        if existing and not args.force:
            print("ABORTED")
            print("-" * 62)
            print(f"  {args.db.name} already contains {existing} player(s).")
            print("  Re-importing would overwrite balances with the JSON values.")
            print("  Re-run with --force if that is genuinely what you want.")
            print()
            return 1

    # ---------- back up before touching anything ----------
    if not args.dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_dir = args.backup_dir / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        backed_up = []
        for name in JSON_FILES:
            path = args.source / name
            if path.exists():
                shutil.copy2(path, backup_dir / name)
                backed_up.append(name)
        if args.db.exists():
            shutil.copy2(args.db, backup_dir / args.db.name)
            backed_up.append(args.db.name)
        print("BACKUP")
        print("-" * 62)
        print(f"  {backup_dir}")
        print(f"  copied: {', '.join(backed_up) if backed_up else '(nothing to copy)'}")
        print()

    # ---------- schema ----------
    conn = sqlite3.connect(":memory:" if args.dry_run else str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    print("SCHEMA")
    print("-" * 62)
    version = migrations.apply_sync(conn)
    print(f"  database at schema v{version}")
    print()

    # ---------- import ----------
    report = {"players": 0, "inventory": 0, "warnings": 0, "guilds": 0,
              "self_roles": 0, "banned_words": 0, "reminders": 0, "skipped": []}

    import_players(conn, economy, report)
    import_config(conn, config, report)      # before warnings: guild rows first
    import_warnings(conn, warnings, report)
    import_reminders(conn, reminders, report)
    conn.commit()

    # ---------- verify against the database itself ----------
    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    db_players = count("SELECT COUNT(*) FROM players")
    db_balance = count("SELECT COALESCE(SUM(balance), 0) FROM players")
    db_xp = count("SELECT COALESCE(SUM(xp), 0) FROM players")
    db_inventory = count("SELECT COUNT(*) FROM inventory")
    db_warnings = count("SELECT COUNT(*) FROM warnings WHERE active = 1")
    db_guilds = count("SELECT COUNT(*) FROM guild_config")
    db_self_roles = count("SELECT COUNT(*) FROM guild_self_roles")
    db_banned = count("SELECT COUNT(*) FROM guild_banned_words")
    db_reminders = count("SELECT COUNT(*) FROM reminders")
    db_shop = count("SELECT COUNT(*) FROM shop_items")

    rows = [
        ("players", src_players, db_players),
        ("total balance (GB $RAM)", src_balance, db_balance),
        ("total xp", src_xp, db_xp),
        ("inventory items", src_inventory, db_inventory),
        ("warnings", src_warnings, db_warnings),
        ("guild configs", src_guilds, db_guilds),
        ("self-assignable roles", None, db_self_roles),
        ("custom banned words", None, db_banned),
        ("reminders", src_reminders, db_reminders),
    ]

    print("VERIFICATION")
    print("-" * 62)
    print(f"  {'':<26} {'JSON':>10} {'SQLITE':>10}   {'':<6}")
    ok = True
    for label, src, dst in rows:
        if src is None:
            print(f"  {label:<26} {'-':>10} {dst:>10}")
            continue
        match = src == dst
        ok = ok and match
        print(f"  {label:<26} {src:>10} {dst:>10}   {'OK' if match else 'MISMATCH'}")
    print()
    print(f"  shop catalogue seeded:   {db_shop} items")
    print()

    if report["skipped"]:
        print("SKIPPED RECORDS")
        print("-" * 62)
        for line in report["skipped"]:
            print(f"  ! {line}")
        print()

    print("RESULT")
    print("-" * 62)
    if ok:
        print("  All source records accounted for. No data lost.")
    else:
        print("  MISMATCH -- do not proceed. Check the skipped records above.")

    if args.dry_run:
        print("  Dry run: in-memory database discarded, nothing written to disk.")
        print("  Re-run without --dry-run to perform the real import.")
    else:
        print(f"  Written to {args.db}")
        print("  Your .json files were left untouched.")
    print()

    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
