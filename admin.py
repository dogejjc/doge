import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLAYERS_FILE = Path("players.json")
PLAYERS_EXAMPLE_FILE = Path("players.example.json")
LIVE_FILE = Path("live.json")
LIVE_EXAMPLE_FILE = Path("live.example.json")
IMPORT_DIR = Path("import")
TZ = ZoneInfo("Asia/Shanghai")


def read_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is None:
        raise FileNotFoundError(path)
    return default


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_data_files():
    if not PLAYERS_FILE.exists():
        if PLAYERS_EXAMPLE_FILE.exists():
            PLAYERS_FILE.write_text(PLAYERS_EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            write_json(PLAYERS_FILE, {"players": [], "alias_history": []})
    if not LIVE_FILE.exists():
        if LIVE_EXAMPLE_FILE.exists():
            LIVE_FILE.write_text(LIVE_EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            write_json(LIVE_FILE, {"current_season": 9, "snapshots": [], "manual_records": []})


def ensure_owner():
    actor = os.environ.get("GITHUB_ACTOR", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not actor or not owner or actor != owner:
        raise PermissionError("Only the repository owner may change manual data")
    return "owner"


def parse_time(value):
    if not str(value or "").strip():
        return datetime.now(TZ).isoformat(timespec="seconds")
    normalized = str(value).strip().replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.isoformat(timespec="seconds")


def parse_season(value, fallback=9):
    text = str(value or "").strip().upper()
    if text.startswith("S"):
        text = text[1:]
    return int(text or fallback)


def find_player(config, player_id):
    player = next(
        (item for item in config.get("players", []) if item.get("id") == player_id or item.get("name") == player_id),
        None,
    )
    if not player:
        raise ValueError(f"Unknown player: {player_id}")
    return player


def safe_import_path(value):
    path = Path(value or "").as_posix().lstrip("/")
    candidate = Path(path)
    if not path.startswith("import/") or ".." in candidate.parts:
        raise ValueError("Import file must be placed under import/")
    if not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def add_score(args, actor):
    ensure_data_files()
    config = read_json(PLAYERS_FILE)
    live = read_json(LIVE_FILE)
    player = find_player(config, args.player)
    score = int(args.score)
    if score < 0 or score > 30000:
        raise ValueError("Score is outside the accepted range")
    cutoff = None
    season = parse_season(args.season, live.get("current_season", 9))
    for snapshot in reversed(live.get("snapshots", [])):
        if int(snapshot.get("season", 0)) == season:
            cutoff = snapshot.get("cutoff500")
            break
    record = {
        "captured_at": parse_time(args.time),
        "season": season,
        "player_id": player["id"],
        "score": score,
        "rank": None,
        "found": False,
        "cutoff500": cutoff,
        "source": "manual",
        "entered_by": actor,
        "note": args.note.strip(),
    }
    records = live.setdefault("manual_records", [])
    duplicate = any(
        item.get("captured_at") == record["captured_at"]
        and item.get("player_id") == record["player_id"]
        and item.get("score") == record["score"]
        for item in records
    )
    if duplicate:
        raise ValueError("The same manual record already exists")
    records.append(record)
    records.sort(key=lambda item: item.get("captured_at", ""))
    write_json(LIVE_FILE, live)
    print(json.dumps(record, ensure_ascii=False))


def add_alias(args, actor):
    ensure_data_files()
    config = read_json(PLAYERS_FILE)
    player = find_player(config, args.player)
    alias = args.alias.strip()
    if not alias:
        raise ValueError("New alias is empty")
    for item in config.get("players", []):
        if alias in item.get("aliases", []) and item.get("id") != player.get("id"):
            raise ValueError("This alias already belongs to another player")
    if alias not in player.setdefault("aliases", []):
        player["aliases"].append(alias)
        config.setdefault("alias_history", []).append({
            "player_id": player["id"],
            "alias": alias,
            "added_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "entered_by": actor,
        })
        write_json(PLAYERS_FILE, config)
    print(f"Alias registered: {player['name']} <- {alias}")


def merge_missing(existing, incoming):
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return existing
    for key, value in incoming.items():
        if key not in existing or existing[key] is None:
            existing[key] = value
        elif isinstance(existing[key], dict) and isinstance(value, dict):
            merge_missing(existing[key], value)
    return existing


def merge_players(current, incoming):
    by_id = {p.get("id"): p for p in current.get("players", []) if p.get("id")}
    for imported in incoming.get("players", []):
        pid = imported.get("id")
        if not pid:
            continue
        if pid not in by_id:
            current.setdefault("players", []).append(imported)
            by_id[pid] = imported
            continue
        target = by_id[pid]
        for alias in imported.get("aliases", []):
            if alias not in target.setdefault("aliases", []):
                target["aliases"].append(alias)
        for key in ("name", "color"):
            if not target.get(key) and imported.get(key):
                target[key] = imported[key]
    seen = {(x.get("player_id"), x.get("alias"), x.get("added_at")) for x in current.get("alias_history", [])}
    for item in incoming.get("alias_history", []):
        key = (item.get("player_id"), item.get("alias"), item.get("added_at"))
        if key not in seen:
            current.setdefault("alias_history", []).append(item)
            seen.add(key)
    return current


def merge_live(current, incoming):
    current["current_season"] = max(int(current.get("current_season", 9)), int(incoming.get("current_season", 9)))
    for meta in ("last_checked_at", "last_data_change_at"):
        if incoming.get(meta) and (not current.get(meta) or incoming[meta] > current[meta]):
            current[meta] = incoming[meta]

    snapshots = current.setdefault("snapshots", [])
    by_key = {(int(s.get("season", 0)), s.get("captured_at")): s for s in snapshots}
    for imported in incoming.get("snapshots", []):
        key = (int(imported.get("season", 0)), imported.get("captured_at"))
        if key in by_key:
            merge_missing(by_key[key], imported)
        else:
            snapshots.append(imported)
            by_key[key] = imported
    snapshots.sort(key=lambda s: (int(s.get("season", 0)), s.get("captured_at", "")))

    records = current.setdefault("manual_records", [])
    seen = {(int(r.get("season", 0)), r.get("captured_at"), r.get("player_id"), r.get("score")) for r in records}
    for imported in incoming.get("manual_records", []):
        key = (int(imported.get("season", 0)), imported.get("captured_at"), imported.get("player_id"), imported.get("score"))
        if key not in seen:
            records.append(imported)
            seen.add(key)
    records.sort(key=lambda r: (int(r.get("season", 0)), r.get("captured_at", ""), r.get("player_id", "")))
    return current


def restore_json(args):
    ensure_data_files()
    source = safe_import_path(args.file)
    payload = read_json(source)
    incoming_live = payload.get("live", payload if "snapshots" in payload else {})
    incoming_players = payload.get("players") or payload.get("config")
    live = merge_live(read_json(LIVE_FILE), incoming_live)
    write_json(LIVE_FILE, live)
    if incoming_players:
        config = merge_players(read_json(PLAYERS_FILE), incoming_players)
        write_json(PLAYERS_FILE, config)
    print(f"Merged JSON backup from {source}: {len(incoming_live.get('snapshots', []))} snapshots, {len(incoming_live.get('manual_records', []))} manual records")


def import_csv(args):
    ensure_data_files()
    source = safe_import_path(args.file)
    config = read_json(PLAYERS_FILE)
    live = read_json(LIVE_FILE)
    player_by_name = {p.get("name"): p.get("id") for p in config.get("players", [])}
    player_ids = {p.get("id") for p in config.get("players", [])}
    groups = {}
    manual = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            captured = parse_time(row.get("记录时间") or row.get("captured_at") or "")
            season = parse_season(row.get("赛季") or row.get("season"), live.get("current_season", 9))
            pid = (row.get("主播ID") or row.get("player_id") or "").strip()
            if pid not in player_ids:
                pid = player_by_name.get((row.get("主播") or row.get("player") or "").strip())
            if not pid:
                continue
            source_name = (row.get("来源") or row.get("source") or "").strip()
            score_text = (row.get("分数") or row.get("score") or "").strip()
            rank_text = (row.get("排名") or row.get("rank") or "").strip()
            cutoff_text = (row.get("前500进榜线") or row.get("cutoff500") or "").strip()
            first_text = (row.get("第一名分数") or row.get("first_place_score") or "").strip()
            first_name = (row.get("第一名ID") or row.get("first_place_name") or "").strip() or None
            total_text = (row.get("榜单人数") or row.get("leaderboard_total") or "").strip()
            score = int(float(score_text)) if score_text else None
            cutoff = int(float(cutoff_text)) if cutoff_text else None
            first_score = int(float(first_text)) if first_text else None
            total = int(float(total_text)) if total_text else None
            if "手动" in source_name or source_name.lower() == "manual":
                if score is not None:
                    manual.append({"captured_at": captured, "season": season, "player_id": pid, "score": score, "rank": None, "found": False, "cutoff500": cutoff, "source": "manual", "entered_by": "import", "note": "CSV恢复"})
                continue
            key = (season, captured)
            snap = groups.setdefault(key, {"captured_at": captured, "season": season, "first_place_score": first_score, "first_place_name": first_name, "cutoff500": cutoff, "leaderboard_total": total, "players": {}})
            if snap.get("first_place_score") is None and first_score is not None:
                snap["first_place_score"] = first_score
            if not snap.get("first_place_name") and first_name:
                snap["first_place_name"] = first_name
            if snap.get("cutoff500") is None and cutoff is not None:
                snap["cutoff500"] = cutoff
            if snap.get("leaderboard_total") is None and total is not None:
                snap["leaderboard_total"] = total
            status = (row.get("榜单状态") or row.get("found") or "").strip()
            found = status in ("上榜", "true", "True", "1") or bool(rank_text)
            snap["players"][pid] = {"found": found, "rank": int(float(rank_text)) if rank_text else None, "score": score if found else None, "matched_alias": None}

    incoming = {"current_season": max([live.get("current_season", 9)] + [s for s, _ in groups.keys()]), "snapshots": list(groups.values()), "manual_records": manual}
    merge_live(live, incoming)
    write_json(LIVE_FILE, live)
    print(f"Merged CSV from {source}: {len(groups)} snapshots, {len(manual)} manual records")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("init")

    score = sub.add_parser("score")
    score.add_argument("--player", required=True)
    score.add_argument("--score", required=True)
    score.add_argument("--time", default="")
    score.add_argument("--season", default="")
    score.add_argument("--note", default="")

    alias = sub.add_parser("alias")
    alias.add_argument("--player", required=True)
    alias.add_argument("--alias", required=True)

    restore = sub.add_parser("restore-json")
    restore.add_argument("--file", required=True)
    csv_import = sub.add_parser("import-csv")
    csv_import.add_argument("--file", required=True)

    args = parser.parse_args()
    if args.operation == "init":
        ensure_data_files()
        print("Data files ready")
        return
    actor = ensure_owner()
    if args.operation == "score":
        add_score(args, actor)
    elif args.operation == "alias":
        add_alias(args, actor)
    elif args.operation == "restore-json":
        restore_json(args)
    elif args.operation == "import-csv":
        import_csv(args)


if __name__ == "__main__":
    main()
