import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLAYERS_FILE = Path("players.json")
LIVE_FILE = Path("live.json")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_owner():
    actor = os.environ.get("GITHUB_ACTOR", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not actor or not owner or actor != owner:
        raise PermissionError("Only the repository owner may change manual data")
    return "owner"


def parse_time(value):
    if not value.strip():
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    normalized = value.strip().replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.isoformat(timespec="seconds")


def find_player(config, player_id):
    player = next(
        (item for item in config.get("players", []) if item.get("id") == player_id or item.get("name") == player_id),
        None,
    )
    if not player:
        raise ValueError(f"Unknown player: {player_id}")
    return player


def add_score(args, actor):
    config = read_json(PLAYERS_FILE)
    live = read_json(LIVE_FILE)
    player = find_player(config, args.player)
    score = int(args.score)
    if score < 0 or score > 20000:
        raise ValueError("Score is outside the accepted range")
    cutoff = None
    season = int(args.season or live.get("current_season", 9))
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
            "added_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "entered_by": actor,
        })
        write_json(PLAYERS_FILE, config)
    print(f"Alias registered: {player['name']} <- {alias}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    score = sub.add_parser("score")
    score.add_argument("--player", required=True)
    score.add_argument("--score", required=True)
    score.add_argument("--time", default="")
    score.add_argument("--season", default="")
    score.add_argument("--note", default="")
    alias = sub.add_parser("alias")
    alias.add_argument("--player", required=True)
    alias.add_argument("--alias", required=True)
    args = parser.parse_args()
    actor = ensure_owner()
    add_score(args, actor) if args.operation == "score" else add_alias(args, actor)


if __name__ == "__main__":
    main()
