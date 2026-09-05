import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = "https://webapi.blizzard.cn/hs-rank-api-server/api/game/ranks"
MODE = "undergroundarena"
PAGE_SIZE = 25
MAX_PAGES = 20
FIRST_SEASON = 9
PLAYERS_FILE = Path("players.json")
LIVE_FILE = Path("live.json")


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def fetch_page(season, page, attempts=3, timeout=30):
    query = urlencode({"page": page, "page_size": PAGE_SIZE, "mode_name": MODE, "season_id": season})
    request = Request(f"{API}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if payload.get("code") != 0:
                raise RuntimeError(payload.get("message", "API error"))
            return payload.get("data") or {"list": [], "total": 0}
        except Exception as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1))
    raise last_error


def detect_season(live):
    season = max(FIRST_SEASON, int(live.get("current_season", FIRST_SEASON)))
    while True:
        try:
            following = fetch_page(season + 1, 1, attempts=1, timeout=10)
        except Exception:
            return season
        if not following.get("list"):
            return season
        season += 1


def fetch_leaderboard(season):
    pages = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {pool.submit(fetch_page, season, page): page for page in range(1, MAX_PAGES + 1)}
        for job in as_completed(jobs):
            pages[jobs[job]] = job.result()
    rows = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        data = pages[page]
        total = max(total, int(data.get("total") or 0))
        rows.extend(data.get("list") or [])
    cutoff = next((row.get("score") for row in rows if row.get("position") == 500), None)
    return rows, total, cutoff


def make_snapshot(season, players, rows, total, cutoff):
    alias_map = {}
    for player in players:
        for alias in player.get("aliases", []):
            alias_map[alias.strip()] = player["id"]
    matched = {}
    for row in rows:
        tag = str(row.get("battle_tag", "")).strip()
        player_id = alias_map.get(tag)
        if player_id and player_id not in matched:
            matched[player_id] = row
    states = {}
    for player in players:
        row = matched.get(player["id"])
        states[player["id"]] = {
            "found": row is not None,
            "rank": row.get("position") if row else None,
            "score": row.get("score") if row else None,
            "matched_alias": row.get("battle_tag") if row else None,
        }
    return {
        "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "season": season,
        "cutoff500": cutoff,
        "leaderboard_total": total,
        "players": states,
    }


def same_state(previous, current):
    keys = ("season", "cutoff500", "leaderboard_total", "players")
    return all(previous.get(key) == current.get(key) for key in keys)


def main():
    config = read_json(PLAYERS_FILE, {"players": []})
    live = read_json(LIVE_FILE, {"current_season": FIRST_SEASON, "snapshots": [], "manual_records": []})
    if not config.get("players"):
        raise RuntimeError("players.json has no players")
    season = detect_season(live)
    rows, total, cutoff = fetch_leaderboard(season)
    snapshot = make_snapshot(season, config["players"], rows, total, cutoff)
    live["current_season"] = season
    snapshots = live.setdefault("snapshots", [])
    if snapshots and same_state(snapshots[-1], snapshot):
        print("No leaderboard change")
        return
    snapshots.append(snapshot)
    live.setdefault("manual_records", [])
    LIVE_FILE.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(snapshot, ensure_ascii=False))


if __name__ == "__main__":
    main()
