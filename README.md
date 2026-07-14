# Spotify On Repeat Sync

Combines everyone's "songs you love right now" into one (or more) shared playlist(s), automatically. Runs as a standalone script on your computer or NAS.

> **Important - why "top tracks" instead of "On Repeat"?** Since Spotify's February 2026 API changes, no app can read the contents of any of Spotify's own algorithmic playlists anymore (On Repeat, Discover Weekly, Release Radar, Daily Mixes) - not even the account owner. That's a deliberate, permanent restriction on Spotify's side, not something we can work around. Instead, this project uses `/me/top/tracks` with `time_range=short_term` (~last 4 weeks) - Spotify's own "top tracks" endpoint, which does still work and is the closest available stand-in for On Repeat.

## Quick start (interactive CLI)

The easiest way to manage everything is the interactive menu:

```bash
python cli.py
```

```
╔══════════════════════════════════════╗
║  Spotify On Repeat Sync              ║
║  Shared playlists from top tracks    ║
╚══════════════════════════════════════╝

  1  Dry-run (preview changes, no modifications)
  2  Sync now (apply changes to Spotify)
  3  Add a user (run Spotify OAuth login)
  4  View playlist status
  5  Manage users & playlists (edit config)

  0  Exit
```

From here you can add users, create playlists, preview syncs, and check who has how many tracks — without touching JSON or remembering flags.

> **For the NAS / scheduled sync:** keep using `python sync.py` (or `python sync.py --playlist NAME`) in Task Scheduler. The interactive CLI is for setup and management — the non-interactive script is for automation.

## How it works

For every playlist in `config.json`:

1. **Read** - pulls each member's top tracks over the last ~4 weeks (`short_term`).
   To provide enough unique material for a full playlist even when members
   have overlapping taste, the script fetches **100 tracks per person**
   (2 pages of 50 via Spotify's `offset` parameter).
2. **Track** - a local SQLite database (`spotify_sync.db`) keeps, per track, who added it and when it was last seen in someone's top tracks (`last_seen`).
   - Track already in the database? -> `last_seen` gets bumped to today.
   - New track, and there's room? -> added.
   - New track, but that person is at their `max_per_user`? -> the oldest track from **that same person** (that wasn't seen again today) gets evicted.
   - New track, playlist is at `max_total`? -> the oldest track from **anyone** (that wasn't seen again today) gets evicted.
   - Important: this only happens **after** everyone's top tracks have been read and candidates are processed **round-robin** (one track per user). That way nobody loses a slot just because someone else happened to be processed earlier, and no single user always gets first pick of shared tracks.
3. **Update** - the real Spotify playlist gets updated with a delta: only add what's new and remove what fell off. The order of everything else stays untouched.

Adding a new playlist combination (e.g. with a third friend) = a new block in `config.json`, no code changes — or use the interactive menu (`python cli.py` → option 5). See `config.example.json` for an example with two playlists.

## Requirements

- Python 3.9 or higher
- `pip install -r requirements.txt` (spotipy, python-dotenv, rich)
- A free Spotify Developer account (for 1 "app" - see below)
- Whoever creates the Spotify app (step 1) needs Spotify **Premium** for that - that's been a requirement from Spotify for "Development Mode" apps since February 2026. Everyone else doesn't need Premium, they just log in against that one app.

## Setup

### 1. Create one Spotify app

This only needs to happen once, for the whole project - not per person.

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in.
2. **Create app**. Name/description don't matter.
3. Under **Redirect URIs**: add `http://127.0.0.1:8888/callback`.
4. Enable "Web API" under the requested APIs.
5. Go to **User Management**, Add all users you want to sync with.
5. Once created, you'll see **Client ID** and **Client Secret** (secret is behind "View client secret").

### 2. Set up the project

```bash
cd spotify-on-repeat-sync
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy the example files:

```bash
cp .env.example .env
cp config.example.json config.json
```

Fill in `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env` from step 1.

### 3. Everyone logs in once

Run this on a regular computer with a browser (doesn't need to be the Synology itself):

```bash
python auth.py --user you
python auth.py --user friend
```

Each time, you'll get a URL. Open it, log in **as that specific person**, click agree, and paste the URL you land on (even though it looks "broken") back into the terminal. This automatically adds a line like `REFRESH_TOKEN_YOU=...` to `.env`.


### 4. Create the shared playlist(s)

In Spotify itself, create an empty playlist for each shared playlist you want (e.g. "You + Friend"). Right-click -> Share -> Copy link to playlist.

Put that link in `.env`, as `PLAYLIST_ID_<NAME>` - where `<NAME>` is the `"name"` field you'll use for that playlist in `config.json` in a moment (uppercased, spaces/dashes become `_`). For a playlist with `"name": "you_and_friend"`, that becomes:
```
PLAYLIST_ID_YOU_AND_FRIEND=https://open.spotify.com/playlist/xxxxxxxxxxxxx
```

Playlist links deliberately live in `.env` rather than `config.json` - that way `config.json` can safely sit in a (git) repo without exposing your real playlist links.

### 5. Fill in `config.json`

```json
{
  "users": [
    { "id": "you", "display_name": "You", "top_tracks_limit": 50 },
    { "id": "friend", "display_name": "Friend", "top_tracks_limit": 50 }
  ],
  "playlists": [
    {
      "name": "you_and_friend",
      "owner_user_id": "you",
      "members": ["you", "friend"],
      "max_total": 100,
      "max_per_user": 50
    }
  ]
}
```

- `owner_user_id` must be someone who's logged in via `auth.py` - their account is used to actually edit the playlist (adding/removing tracks).
- `max_per_user` is a fairness cap (no one person can fill the whole playlist), `max_total` is the real ceiling. `max_per_user × number of members` can be higher than `max_total` just fine - `max_total` simply applies as the hard limit.
- Optional, per person under `users`: `"top_tracks_time_range"` (`short_term`/`medium_term`/`long_term`, default `short_term`) and `"top_tracks_limit"` (default 30, max 50; **recommended**: 50 for a 2-person playlist of 100) if you want to tune someone's window. Regardless of this limit, the script always fetches **2 pages** (via `offset`) so the effective pool is up to `2 × limit` tracks per user - enough to fill a 100-track playlist even with significant overlap.

### 6. Test it

```bash
python sync.py --dry-run                     # preview everything
python sync.py --dry-run --playlist friend_group  # preview one playlist
```

This logs exactly what would happen, without touching the database or the real Spotify playlist. Happy with it? Run it again without `--dry-run`.

Use `--playlist NAME` to sync only a specific playlist. Repeat the flag for multiple playlists:

```bash
python sync.py --playlist you_and_friend              # just one
python sync.py --playlist you_and_friend --playlist friend_group  # two
```

Without `--playlist`, all playlists from `config.json` are synced.

Logs land in `logs/sync_<date>.log` (and also just print to the screen).

> **Pro tip:** use the interactive CLI for all of the above — `python cli.py` wraps dry-run, sync, user management, and config editing in one menu. No flags to remember.

## Interactive CLI vs. script

| What | Use |
|---|---|
| Setup: add users, create playlists, check status | `python cli.py` (interactive) |
| Automated sync (daily cron / NAS task) | `python sync.py` (non-interactive) |

Both share the same database and config files — switch freely between them.

## Easy to extend

What keeps this deliberately simple, so it's easy to grow:
- Adding a new source = a new user + optionally a new playlist block in `config.json`. `sync.py` itself never needs to change for more people or more playlist combinations.
- `spotify_client.py`, `database.py`, and `sync.py` are decoupled - if you ever want a different data source (e.g. a different `time_range`, or something else entirely), you only need to change `get_top_track_ids()` in `spotify_client.py`.
- The number of pages fetched is controlled by `TOP_TRACKS_PAGES` in `spotify_client.py` (default: 2). Tune this if members have very broad or very narrow listening habits.
- Possible next steps: auto-creating playlists instead of doing it by hand, or a small overview (standalone script or webpage) showing what's currently in each database.
