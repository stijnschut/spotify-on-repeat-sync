# Spotify On Repeat Sync

Combines everyone's "songs you love right now" into one (or more) shared playlist(s), automatically, refreshed every day. Runs as a standalone script on your Synology via Task Scheduler.

> **Important - why "top tracks" instead of "On Repeat"?** Since Spotify's February 2026 API changes, no app can read the contents of any of Spotify's own algorithmic playlists anymore (On Repeat, Discover Weekly, Release Radar, Daily Mixes) - not even the account owner. That's a deliberate, permanent restriction on Spotify's side, not something we can work around. Instead, this project uses `/me/top/tracks` with `time_range=short_term` (~last 4 weeks) - Spotify's own "top tracks" endpoint, which does still work and is the closest available stand-in for On Repeat.

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
   - Important: this only happens **after** everyone's top tracks have been read and candidates are processed **round-robin** (one track per user,轮流). That way nobody loses a slot just because someone else happened to be processed earlier, and no single user always gets first pick of shared tracks.
3. **Update** - the real Spotify playlist gets updated with a delta: only add what's new and remove what fell off. The order of everything else stays untouched.

Adding a new playlist combination (e.g. with a third friend) = a new block in `config.json`, no code changes. See `config.example.json` for an example with two playlists.

## Requirements

- Python 3.9 or higher
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

> The codes Spotify gives are only valid briefly - if login fails, just re-run the command and paste the URL back a bit faster.

> Already logged in with an older version of this project (before the top-tracks switch)? Re-run `auth.py` for that person - there's an extra scope (`user-top-read`) that an old token won't have.

Updating this on your own laptop and need it on the NAS? Just copy the updated `.env` over (or add the missing line by hand).

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
python sync.py --dry-run
```

This logs exactly what would happen, without touching the database or the real Spotify playlist. Happy with it? Run it again without `--dry-run`.

Logs land in `logs/sync_<date>.log` (and also just print to the screen).

### 7. Set up Synology Task Scheduler

**Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined script**

- **General**: name e.g. "Spotify Sync", a user with read/write access to the project folder.
- **Schedule**: daily, e.g. 04:00 at night - well before anyone's awake, and top tracks don't change in real time anyway.
- **Task Settings** -> User-defined script:

```bash
cd /volume1/path/to/spotify-on-repeat-sync
./venv/bin/python3 sync.py
```

Check via SSH (`which python3`, or the path to your venv) which path is correct for your DSM.

You don't need to worry about Spotify API rate limits with this kind of usage (a handful of people, once a day) - that's well under anything that would ever become a problem.

## Adding someone (e.g. a second friend)

1. They run it themselves: `python auth.py --user friend2` -> adds `REFRESH_TOKEN_FRIEND2` to `.env`.
2. Add them to `users` in `config.json`:
   ```json
   { "id": "friend2", "display_name": "Friend2" }
   ```
3. Pick one of these two, depending on what you want:
   - **Add them to the existing playlist**: add `"friend2"` to that playlist's `members`, and raise `max_total` if you want more room.
   - **Separate, bigger playlist including them** (e.g. 100 with just two of you, 150 once a third joins): add a new block to `playlists` with its own `name` and `members: ["you", "friend", "friend2"]`, create a new empty playlist in Spotify, and put that link in `.env` as `PLAYLIST_ID_<NAME>`. The original playlist keeps existing separately.

No code changes needed in either case.

## Adjusting settings

Everything lives in `config.json`, per playlist:
- `max_total` - hard ceiling for that playlist.
- `max_per_user` - how many tracks from one person can be in there at most.

Prefer no limit at all ("infinity and beyond")? Just set `max_total` and `max_per_user` to a high number (e.g. `999999`) - in practice nothing will ever get removed.

## Troubleshooting

- **Duplicates keep getting added to the shared playlist every run** — the delta log shows `+N` tracks even though they're already in the DB. This usually means the script can't read the playlist's current tracks. Since early 2025, Spotify's API uses `"item"` (not `"track"`) as the key in playlist item responses. Make sure `get_playlist_track_ids()` in `spotify_client.py` reads `item.get("item")` rather than `item.get("track")`.
- **404/403 when reading a playlist** - if this happens on the *shared* playlist itself (not while fetching top tracks), check that `owner_user_id` is correct and that person has `playlist-modify-*` scopes (re-running `auth.py` usually fixes this).
- **"Missing REFRESH_TOKEN_X in .env"** - that person hasn't (successfully) run `auth.py` yet.
- **"Missing PLAYLIST_ID_X in .env"** - that playlist is missing its `PLAYLIST_ID_<NAME>` line in `.env` (see step 4 of the setup).
- **Someone's top tracks look empty or very short** - `/me/top/tracks` needs some listening history to return anything meaningful; with little recent activity the list can be short. This resolves itself with more listening time.
- Every error is caught and logged per person/playlist - if one account has a problem, the rest of the sync just continues. Check `logs/` for the full story.
- `debug_list_playlists.py` in this project is a standalone debug script (`python debug_list_playlists.py --user X`) that lists all of an account's playlists with their owner - useful when you're unsure about a playlist ID.

## Easy to extend

What keeps this deliberately simple, so it's easy to grow:
- Adding a new source = a new user + optionally a new playlist block in `config.json`. `sync.py` itself never needs to change for more people or more playlist combinations.
- `spotify_client.py`, `database.py`, and `sync.py` are decoupled - if you ever want a different data source (e.g. a different `time_range`, or something else entirely), you only need to change `get_top_track_ids()` in `spotify_client.py`.
- The number of pages fetched is controlled by `TOP_TRACKS_PAGES` in `spotify_client.py` (default: 2). Tune this if members have very broad or very narrow listening habits.
- Possible next steps: auto-creating playlists instead of doing it by hand, or a small overview (standalone script or webpage) showing what's currently in each database.
