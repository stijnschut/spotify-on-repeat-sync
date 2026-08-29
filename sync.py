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
    python sync.py                        # normal run
    python sync.py --dry-run              # log what WOULD happen, change nothing
    python sync.py --playlist friend_group    # only one playlist
    python sync.py --playlist a --playlist b  # specific playlists
    python sync.py --playlist friend_group --dry-run  # dry-run one playlist
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
from notify import send_patch_notes
from spotify_client import (
    add_tracks,
    get_app_credentials,
    get_client_for_user,
    get_playlist_tracks,
    get_top_tracks,
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
        config = json.load(f)

    # Validate structure so typos / missing fields fail with a clear message.
    if "users" not in config or not isinstance(config["users"], list):
        raise ValueError("config.json must contain a 'users' list")
    if "playlists" not in config or not isinstance(config["playlists"], list):
        raise ValueError("config.json must contain a 'playlists' list")

    for i, user in enumerate(config["users"]):
        if "id" not in user:
            raise ValueError(f"User at index {i} is missing required 'id' field: {json.dumps(user)}")

    for playlist in config["playlists"]:
        for field in ("name", "owner_user_id", "members", "max_total", "max_per_user"):
            if field not in playlist:
                raise ValueError(
                    f"Playlist is missing required field '{field}': {json.dumps(playlist)}"
                )
        if not isinstance(playlist["members"], list):
            raise ValueError(
                f"Playlist '{playlist['name']}': 'members' must be a list"
            )
        for member in playlist["members"]:
            if not isinstance(member, str):
                raise ValueError(
                    f"Playlist '{playlist['name']}': member {member!r} should be a string (user id)"
                )

    # Warn about duplicate playlist names (they share the same DB namespace).
    names = [p["name"] for p in config["playlists"]]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        logger.warning(
            "Duplicate playlist names detected: %s - they will share the same tracks in the database",
            ", ".join(sorted(dupes)),
        )

    # Optional global filters.
    blacklist = config.get("artist_blacklist")
    if blacklist is not None and not isinstance(blacklist, list):
        raise ValueError("'artist_blacklist' must be a list of artist names")
    for a in blacklist or []:
        if not isinstance(a, str):
            raise ValueError(f"'artist_blacklist' entries must be strings, got {a!r}")

    max_dur = config.get("max_duration_minutes")
    if max_dur is not None and not isinstance(max_dur, int):
        raise ValueError("'max_duration_minutes' must be an integer or null")

    return config


def get_user_credentials(user_id: str, app: str = "app1") -> tuple[str, str, str]:
    """Look up (client_id, client_secret, refresh_token) for a user.

    Uses the Spotify app that this user belongs to (see the "app" field
    in config.json). The refresh token is app-specific, so the two must
    match.
    """
    client_id, client_secret = get_app_credentials(app)
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


def discord_webhook_env_key(playlist_name: str) -> str:
    """Return the .env key for a playlist's optional Discord webhook URL."""
    return "DISCORD_WEBHOOK_" + re.sub(r"[^A-Za-z0-9]", "_", playlist_name).upper()


def get_discord_webhook(playlist_name: str) -> str | None:
    """Return the Discord webhook URL for a playlist, or None if unset."""
    return os.environ.get(discord_webhook_env_key(playlist_name))


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
    - Already added this run?            -> another user already
                                             claimed it (shared track),
                                             just bump last_seen.
    """
    # Guard: another user may have already added this exact track in
    # the current run (e.g. both users have the same brand-new track
    # in their top tracks). Without this, the second db.add_track()
    # would hit a PRIMARY KEY violation.
    if db.track_exists(playlist_name, track_id):
        if not dry_run:
            db.update_last_seen(playlist_name, track_id, today)
        return

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


def _extract_artist_id(entry: str) -> str | None:
    """
    Extract a Spotify artist ID from a blacklist entry, or return None.
    Accepts an artist URL (open.spotify.com/artist/<id>), a URI
    (spotify:artist:<id>), or a bare 22-char base62 ID. Returns None
    for anything else (treated as a plain name).
    """
    entry = entry.strip()
    if not entry:
        return None
    m = re.search(r"artist/([A-Za-z0-9]+)", entry)
    if m:
        return m.group(1)
    m = re.match(r"^spotify:artist:([A-Za-z0-9]+)$", entry)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", entry):
        return entry
    return None


def _should_skip_track(
    track: dict, artist_blacklist: list[str] | None, max_duration_minutes: int | None
) -> bool:
    """Return True if a track should be ignored (blacklisted artist or too long)."""
    if max_duration_minutes:
        duration_ms = track.get("duration_ms")
        if duration_ms and duration_ms > max_duration_minutes * 60_000:
            return True

    if artist_blacklist:
        blocked_ids: set[str] = set()
        blocked_names: set[str] = set()
        for entry in artist_blacklist:
            if not isinstance(entry, str):
                continue
            artist_id = _extract_artist_id(entry)
            if artist_id:
                blocked_ids.add(artist_id)
            elif entry.strip():
                blocked_names.add(entry.strip().lower())

        # IDs are unambiguous and preferred; names are a case-insensitive fallback.
        if blocked_ids and any(
            aid in blocked_ids for aid in track.get("artist_ids", [])
        ):
            return True
        if blocked_names and any(
            (a or "").strip().lower() in blocked_names for a in track.get("artists", [])
        ):
            return True

    return False


def sync_playlist(
    playlist_cfg: dict,
    users_by_id: dict,
    db: TrackDatabase,
    today: str,
    dry_run: bool,
    artist_blacklist: list[str] | None = None,
    max_duration_minutes: int | None = None,
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
    # track_id -> "Artist - Title" for anything we see this run, used
    # later for the Discord patch notes (avoids a separate metadata API
    # call, which Spotify's 2026 API restricts).
    names_by_id: dict[str, str] = {}

    for user_id in playlist_cfg["members"]:
        user_cfg = users_by_id.get(user_id)
        if not user_cfg:
            logger.warning(
                "  Member '%s' isn't defined under 'users' in config.json - skipping",
                user_id,
            )
            continue

        try:
            app = user_cfg.get("app", "app1")
            client_id, client_secret, refresh_token = get_user_credentials(user_id, app)
            sp = get_client_for_user(client_id, client_secret, refresh_token)
            time_range = user_cfg.get("top_tracks_time_range", "short_term")
            limit = user_cfg.get("top_tracks_limit", 30)
            tracks = get_top_tracks(sp, time_range=time_range, limit=limit)
            logger.info("  %s: %d top tracks (%s)", user_id, len(tracks), time_range)
        except RuntimeError:
            logger.warning("  %s: no token in .env, skipping", user_id)
            continue
        except Exception:
            logger.exception(
                "  Failed to read top tracks for %s - skipping this user for this run",
                user_id,
            )
            continue

        # Capture display names for everything (even skipped tracks, so
        # Discord patch notes still show proper names), then filter out
        # tracks the user doesn't want.
        for t in tracks:
            names_by_id[t["id"]] = t["display"]

        kept = [t for t in tracks if not _should_skip_track(t, artist_blacklist, max_duration_minutes)]
        skipped = len(tracks) - len(kept)
        if skipped:
            logger.info("  %s: filtered out %d track(s) (blacklist/duration)", user_id, skipped)

        track_ids = [t["id"] for t in kept]

        # Use batched refresh: one connection bumps all existing tracks
        # and returns the ones that are new (candidates).
        new_ids = db.refresh_tracks(name, track_ids, today) if not dry_run else [
            tid for tid in track_ids if not db.track_exists(name, tid)
        ]
        for tid in new_ids:
            candidates.append((tid, user_id))

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
        owner_app = users_by_id.get(owner_id, {}).get("app", "app1")
        client_id, client_secret, refresh_token = get_user_credentials(owner_id, owner_app)
        sp_owner = get_client_for_user(client_id, client_secret, refresh_token)
    except Exception:
        logger.exception(
            "  Couldn't authenticate playlist owner '%s' - cannot push updates to Spotify",
            owner_id,
        )
        return

    desired = db.get_all_track_ids(name)
    try:
        current_tracks = get_playlist_tracks(sp_owner, spotify_playlist_id)
    except Exception:
        logger.exception(
            "  Couldn't read the current tracks of playlist '%s' on Spotify - skipping push",
            name,
        )
        return

    current = [tid for tid, _ in current_tracks]
    for tid, display in current_tracks:
        names_by_id.setdefault(tid, display)

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

    # Send Discord patch notes if this playlist has a webhook configured.
    _send_webhook_if_needed(
        db, users_by_id, names_by_id, name, to_add, to_remove
    )


def _send_webhook_if_needed(
    db: TrackDatabase,
    users_by_id: dict,
    names_by_id: dict[str, str],
    playlist_name: str,
    to_add: list[str],
    to_remove: list[str],
) -> None:
    """
    Send Discord patch notes for a playlist's delta, if a webhook is
    configured for that playlist and there is something to report.
    Skips silently (no error) when no webhook is set or nothing changed.
    """
    webhook_url = get_discord_webhook(playlist_name)
    if not webhook_url:
        return
    if not to_add and not to_remove:
        return

    # Resolve display names from the map we already built this run
    # (top tracks + current playlist), so we never need a separate
    # metadata call. Added tracks also get their source_user looked up
    # from the DB, mapped to a friendly display name from the config,
    # then grouped per user for the Discord embed.
    sources = db.get_track_sources(playlist_name, to_add)

    added_grouped: dict[str, list[str]] = {}
    for tid in to_add:
        title = names_by_id.get(tid, tid)
        uid = sources.get(tid, "?")
        display = users_by_id.get(uid, {}).get("display_name", uid)
        added_grouped.setdefault(display, []).append(title)

    removed_lines = [names_by_id.get(tid, tid) for tid in to_remove]

    try:
        send_patch_notes(webhook_url, playlist_name, added_grouped, removed_lines)
        logger.info("  Sent Discord patch notes for '%s'", playlist_name)
    except Exception:
        logger.exception("  Failed to send Discord webhook for '%s'", playlist_name)


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
    parser.add_argument(
        "--playlist",
        action="append",
        dest="playlists",
        help="Only sync this playlist (can be repeated: --playlist a --playlist b). "
             "If omitted, all playlists in config are synced.",
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
    # Global filters: skip blacklisted artists and (optionally) tracks
    # longer than a max duration. Both are optional and default to off.
    artist_blacklist = config.get("artist_blacklist") or []
    max_duration_minutes = config.get("max_duration_minutes")
    db = TrackDatabase(args.db)
    today = date.today().isoformat()

    if args.playlists:
        known = {p["name"] for p in config["playlists"]}
        for name in args.playlists:
            if name not in known:
                logger.warning(
                    "--playlist '%s' doesn't match any playlist in config (%s) - skipping",
                    name,
                    ", ".join(sorted(known)),
                )

    for playlist_cfg in config["playlists"]:
        if args.playlists and playlist_cfg["name"] not in args.playlists:
            continue
        try:
            sync_playlist(
                playlist_cfg,
                users_by_id,
                db,
                today,
                args.dry_run,
                artist_blacklist,
                max_duration_minutes,
            )
        except Exception:
            logger.exception(
                "Playlist '%s' failed - continuing with the next one",
                playlist_cfg.get("name"),
            )

    logger.info("=== Sync run finished ===")


if __name__ == "__main__":
    main()
