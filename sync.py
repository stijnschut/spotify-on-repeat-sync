"""
Main sync script - point Synology Task Scheduler at this file.

For every playlist defined in config.json:
  1. Read each member's top tracks (short-term, our stand-in for On
     Repeat - see spotify_client.py for why On Repeat itself is no
     longer readable via the API).
  2. Track new ones in the database, evicting the stalest track (the
     one with the oldest "last seen in someone's top tracks" date)
     once the playlist - or that one member's own slice of it - is
     full.
  3. Push the resulting set to the real Spotify playlist as a delta:
     only add what's new and remove what fell off, so track order for
     everything else is left untouched.

Usage:
    python sync.py                # normal run
    python sync.py --dry-run      # log what WOULD happen, change nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

from database import TrackDatabase
from dotenv import load_dotenv
from spotify_client import (
    add_tracks,
    get_client_for_user,
    get_playlist_track_ids,
    get_top_track_ids,
    remove_tracks,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("spotify_sync")


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"sync_{date.today().isoformat()}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_user_credentials(user_id: str) -> tuple[str, str, str]:
    """Look up (client_id, client_secret, refresh_token) for a user from the environment."""
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    env_key = f"REFRESH_TOKEN_{user_id.upper()}"
    refresh_token = os.environ.get(env_key)
    if not refresh_token:
        raise RuntimeError(
            f"Missing {env_key} in .env - has '{user_id}' run auth.py yet?"
        )
    return client_id, client_secret, refresh_token


def get_playlist_id(playlist_name: str) -> str:
    """
    Look up a playlist's Spotify ID/URL from .env, e.g. playlist name
    "you_and_friend" -> env var PLAYLIST_ID_YOU_AND_FRIEND. Keeping this
    out of config.json means config.json (which may end up in git)
    never has to contain real playlist links.
    """
    env_key = "PLAYLIST_ID_" + re.sub(r"[^A-Za-z0-9]", "_", playlist_name).upper()
    playlist_id = os.environ.get(env_key)
    if not playlist_id:
        raise RuntimeError(
            f"Missing {env_key} in .env - add the shared playlist's link/ID there"
        )
    return playlist_id


def add_new_track(
    db: TrackDatabase,
    playlist_name: str,
    track_id: str,
    user_id: str,
    max_total: int,
    max_per_user: int,
    today: str,
    dry_run: bool,
) -> None:
    """
    Place one brand-new track (not yet tracked at all) into a playlist
    that's already had ALL of today's "still active" tracks refreshed
    (see pass 1 in sync_playlist). Because that refresh already
    happened, anything still eligible for eviction here has genuinely
    fallen out of everyone's top tracks - not just tracks that simply
    haven't been re-checked yet this run.

    - There's room (both totals)?        -> add it.
    - That user is at their own cap?     -> evict THAT user's own
                                             stalest track, so one
                                             person's new tracks can't
                                             eat someone else's slots.
    - Playlist overall is at max_total?  -> evict the stalest track
                                             in the whole playlist,
                                             regardless of who added it.
    - Nothing old enough to evict?       -> drop the new track for
                                             this run; everything in
                                             that slot is still "hot",
                                             it'll get another chance
                                             next run.
    """
    user_count = db.count_for_user(playlist_name, user_id)
    if user_count >= max_per_user:
        oldest = db.get_oldest(playlist_name, before_date=today, source_user=user_id)
        if not oldest:
            logger.info(
                "  %s is at their cap (%d/%d) and all of them are fresh today - skipping new track %s",
                user_id,
                user_count,
                max_per_user,
                track_id,
            )
            return
        logger.info(
            "  %s at cap (%d/%d): swapping out %s for %s",
            user_id,
            user_count,
            max_per_user,
            oldest["track_id"],
            track_id,
        )
        if not dry_run:
            db.remove_track(playlist_name, oldest["track_id"])
            db.add_track(playlist_name, track_id, user_id, today)
        return

    total_count = db.count_total(playlist_name)
    if total_count >= max_total:
        oldest = db.get_oldest(playlist_name, before_date=today, source_user=None)
        if not oldest:
            logger.info(
                "  Playlist full (%d/%d) and everything is fresh today - skipping new track %s from %s",
                total_count,
                max_total,
                track_id,
                user_id,
            )
            return
        logger.info(
            "  Playlist full (%d/%d): swapping out %s (from %s) for %s (from %s)",
            total_count,
            max_total,
            oldest["track_id"],
            oldest["source_user"],
            track_id,
            user_id,
        )
        if not dry_run:
            db.remove_track(playlist_name, oldest["track_id"])
            db.add_track(playlist_name, track_id, user_id, today)
        return

    logger.info("  Adding new track %s from %s", track_id, user_id)
    if not dry_run:
        db.add_track(playlist_name, track_id, user_id, today)


def sync_playlist(
    playlist_cfg: dict, users_by_id: dict, db: TrackDatabase, today: str, dry_run: bool
) -> None:
    name = playlist_cfg["name"]
    max_total = playlist_cfg["max_total"]
    max_per_user = playlist_cfg["max_per_user"]
    logger.info("Playlist '%s': syncing members %s", name, playlist_cfg["members"])

    # Pass 1: read every member's top tracks. Anything already tracked
    # gets its last_seen bumped to today; anything brand new is set
    # aside as a candidate. Nothing is added or evicted yet - we want
    # every member's "still active today" tracks reflected in the
    # database BEFORE making any eviction decisions, otherwise a
    # member processed later could unfairly lose a track that's still
    # genuinely in their top tracks, just not re-confirmed yet.
    candidates: list[tuple[str, str]] = []

    for user_id in playlist_cfg["members"]:
        user_cfg = users_by_id.get(user_id)
        if not user_cfg:
            logger.warning(
                "  Member '%s' isn't defined under 'users' in config.json - skipping",
                user_id,
            )
            continue

        try:
            client_id, client_secret, refresh_token = get_user_credentials(user_id)
            sp = get_client_for_user(client_id, client_secret, refresh_token)
            time_range = user_cfg.get("top_tracks_time_range", "short_term")
            limit = user_cfg.get("top_tracks_limit", 30)
            track_ids = get_top_track_ids(sp, time_range=time_range, limit=limit)
            logger.info("  %s: %d top tracks (%s)", user_id, len(track_ids), time_range)
        except Exception:
            logger.exception(
                "  Failed to read top tracks for %s - skipping this user for this run",
                user_id,
            )
            continue

        for track_id in track_ids:
            if db.track_exists(name, track_id):
                if not dry_run:
                    db.update_last_seen(name, track_id, today)
            else:
                candidates.append((track_id, user_id))

    # Pass 2: now try to fit each new track in, evicting the stalest
    # qualifying track only if needed - see add_new_track().
    # Process candidates round-robin per user so that no single user's
    # tracks get priority when claiming a shared song's source_user slot.
    by_user: dict[str, list[str]] = {}
    for track_id, user_id in candidates:
        by_user.setdefault(user_id, []).append(track_id)
    _round = 0
    while any(by_user.values()):
        for uid in list(by_user.keys()):
            if by_user[uid]:
                tid = by_user[uid].pop(0)
                add_new_track(
                    db, name, tid, uid, max_total, max_per_user, today, dry_run
                )
        _round += 1
        if _round > 200:
            break

    # Push the resulting desired state to the real playlist as a delta,
    # authenticated as whoever owns/manages that shared playlist.
    try:
        spotify_playlist_id = get_playlist_id(name)
    except RuntimeError:
        logger.exception("  Cannot push updates for '%s'", name)
        return

    owner_id = playlist_cfg["owner_user_id"]
    try:
        client_id, client_secret, refresh_token = get_user_credentials(owner_id)
        sp_owner = get_client_for_user(client_id, client_secret, refresh_token)
    except Exception:
        logger.exception(
            "  Couldn't authenticate playlist owner '%s' - cannot push updates to Spotify",
            owner_id,
        )
        return

    desired = db.get_all_track_ids(name)
    try:
        current = get_playlist_track_ids(sp_owner, spotify_playlist_id)
    except Exception:
        logger.exception(
            "  Couldn't read the current tracks of playlist '%s' on Spotify - skipping push",
            name,
        )
        return

    to_add = [t for t in desired if t not in current]
    to_remove = [t for t in current if t not in desired]

    logger.info(
        "  Delta for '%s': +%d / -%d (target size %d)",
        name,
        len(to_add),
        len(to_remove),
        len(desired),
    )

    if dry_run:
        logger.info("  [dry-run] Not touching the real Spotify playlist")
        return

    if to_remove:
        remove_tracks(sp_owner, spotify_playlist_id, to_remove)
    if to_add:
        add_tracks(sp_owner, spotify_playlist_id, to_add)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync shared Spotify playlists from everyone's top tracks"
    )
    parser.add_argument(
        "--config", default=str(BASE_DIR / "config.json"), help="Path to config.json"
    )
    parser.add_argument(
        "--db",
        default=str(BASE_DIR / "spotify_sync.db"),
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without changing anything",
    )
    args = parser.parse_args()

    setup_logging()
    load_dotenv(BASE_DIR / ".env")

    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get(
        "SPOTIFY_CLIENT_SECRET"
    ):
        logger.error(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing - copy .env.example to .env and fill them in"
        )
        sys.exit(1)

    logger.info("=== Sync run started%s ===", " (dry-run)" if args.dry_run else "")

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(
            "Config not found: %s (copy config.example.json to config.json)",
            config_path,
        )
        sys.exit(1)

    config = load_config(config_path)
    users_by_id = {u["id"]: u for u in config["users"]}
    db = TrackDatabase(args.db)
    today = date.today().isoformat()

    for playlist_cfg in config["playlists"]:
        try:
            sync_playlist(playlist_cfg, users_by_id, db, today, args.dry_run)
        except Exception:
            logger.exception(
                "Playlist '%s' failed - continuing with the next one",
                playlist_cfg.get("name"),
            )

    logger.info("=== Sync run finished ===")


if __name__ == "__main__":
    main()
