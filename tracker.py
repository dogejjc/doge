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
PLAYERS_EXAMPLE_FILE = Path("players.example.json")
LIVE_FILE = Path("live.json")
LIVE_EXAMPLE_FILE = Path("live.example.json")
TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_MINUTES = [0, 30]


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def ensure_file(path, example_path, default):
    if path.exists():
        return
    if example_path.exists():
        path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    def rank_of(row):
        try:
            return int(row.get("position"))
        except (TypeError, ValueError):
            return None

    first_row = next((row for row in rows if rank_of(row) == 1), None)
    cutoff_row = next((row for row in rows if rank_of(row) == 500), None)
    first_score = first_row.get("score") if first_row else None
    first_name = first_row.get("battle_tag") if first_row else None
    cutoff = cutoff_row.get("score") if cutoff_row else None
    return rows, total, cutoff, first_score, first_name


def expire_duplicate_exclusions(live, players, rows, season, captured_at):
    aliases = {player["id"]: set(player.get("aliases", [])) for player in players}
    for rule in live.get("duplicate_exclusions", []):
        if int(rule.get("season", 0)) != season or rule.get("until") or str(rule.get("from", "")) >= captured_at:
            continue
        player_aliases = aliases.get(rule.get("player_id"), set())
        still_present = any(
            str(row.get("battle_tag", "")).strip() in player_aliases
            and int(row.get("score")) == int(rule.get("score"))
            for row in rows
        )
        if not still_present:
            rule["until"] = captured_at


def make_snapshot(season, players, rows, total, cutoff, first_score, first_name, captured_at, previous=None, exclusions=None):
    alias_map = {}
    for player in players:
        for alias in player.get("aliases", []):
            alias_map.setdefault(alias.strip(), []).append(player["id"])
    matched = {player["id"]: [] for player in players}
    for row in rows:
        tag = str(row.get("battle_tag", "")).strip()
        for player_id in alias_map.get(tag, []):
            matched[player_id].append(row)
    states = {}
    for player in players:
        candidates = matched.get(player["id"], [])
        if len(candidates) == 1:
            row = candidates[0]
            states[player["id"]] = {
                "found": True,
                "rank": row.get("position"),
                "score": row.get("score"),
                "matched_alias": row.get("battle_tag"),
            }
        elif len(candidates) > 1:
            ordered = sorted(candidates, key=lambda item: int(item.get("position") or 999999))
            active_exclusions = [
                rule for rule in (exclusions or [])
                if int(rule.get("season", 0)) == season
                and rule.get("player_id") == player["id"]
                and str(rule.get("from", "")) <= captured_at
                and (not rule.get("until") or captured_at < str(rule.get("until")))
            ]
            eligible = [
                row for row in ordered
                if not any(int(rule.get("score")) == int(row.get("score")) for rule in active_exclusions)
            ]
            if not eligible:
                eligible = ordered
            previous_state = (previous or {}).get("players", {}).get(player["id"], {})
            previous_score = previous_state.get("score") if previous_state.get("found") else None
            if isinstance(previous_score, (int, float)):
                chosen = min(
                    eligible,
                    key=lambda item: (
                        abs(int(item.get("score") or 0) - int(previous_score)),
                        int(item.get("position") or 999999),
                    ),
                )
                selection_reason = "closest_previous_score"
            else:
                chosen = eligible[0]
                selection_reason = "highest_rank"
            states[player["id"]] = {
                "found": True,
                "rank": chosen.get("position"),
                "score": chosen.get("score"),
                "matched_alias": chosen.get("battle_tag"),
                "duplicate_selection": "auto",
                "duplicate_selection_reason": selection_reason,
                "duplicate_exclusions_applied": len(ordered) - len(eligible),
                "candidates": [
                    {
                        "rank": row.get("position"),
                        "score": row.get("score"),
                        "matched_alias": row.get("battle_tag"),
                    }
                    for row in ordered
                ],
            }
        else:
            states[player["id"]] = {
                "found": False,
                "rank": None,
                "score": None,
                "matched_alias": None,
            }
    return {
        "captured_at": captured_at,
        "season": season,
        "first_place_score": first_score,
        "first_place_name": first_name,
        "cutoff500": cutoff,
        "leaderboard_total": total,
        "players": states,
    }


def same_state(previous, current):
    keys = ("season", "first_place_score", "first_place_name", "cutoff500", "leaderboard_total", "players")
    return all(previous.get(key) == current.get(key) for key in keys)


def main():
    ensure_file(PLAYERS_FILE, PLAYERS_EXAMPLE_FILE, {"players": [], "alias_history": []})
    ensure_file(LIVE_FILE, LIVE_EXAMPLE_FILE, {"current_season": FIRST_SEASON, "snapshots": [], "manual_records": []})
    config = read_json(PLAYERS_FILE, {"players": []})
    live = read_json(LIVE_FILE, {"current_season": FIRST_SEASON, "snapshots": [], "manual_records": []})
    if not config.get("players"):
        raise RuntimeError("players.json has no players")

    checked_at = datetime.now(TZ).isoformat(timespec="seconds")
    season = detect_season(live)
    rows, total, cutoff, first_score, first_name = fetch_leaderboard(season)
    expire_duplicate_exclusions(live, config["players"], rows, season, checked_at)
    previous = next(
        (item for item in reversed(live.get("snapshots", [])) if int(item.get("season", 0)) == season),
        None,
    )
    snapshot = make_snapshot(
        season,
        config["players"],
        rows,
        total,
        cutoff,
        first_score,
        first_name,
        checked_at,
        previous,
        live.get("duplicate_exclusions", []),
    )

    live["current_season"] = season
    live["last_checked_at"] = checked_at
    live["schedule_minutes"] = SCHEDULE_MINUTES
    snapshots = live.setdefault("snapshots", [])
    previous = snapshots[-1] if snapshots else None
    changed = previous is None or not same_state(previous, snapshot)
    # Every successful check is kept. The front end collapses visually redundant flat points,
    # while the raw history remains available for exact "previous check" and midnight comparisons.
    snapshots.append(snapshot)
    if changed:
        live["last_data_change_at"] = checked_at
    elif not live.get("last_data_change_at") and previous:
        live["last_data_change_at"] = previous.get("captured_at")

    live.setdefault("manual_records", [])
    LIVE_FILE.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if changed:
        print(json.dumps(snapshot, ensure_ascii=False))
    else:
        print(f"Leaderboard checked at {checked_at}; no leaderboard data change")


if __name__ == "__main__":
    main()
