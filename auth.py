"""
One-time interactive OAuth login - run this once per person before the
main sync script can read their top tracks / manage a shared playlist
on their behalf.

Usage:
    python auth.py --user you

Run this on a machine with a normal browser - it does NOT need to run
on the Synology itself. It stores the resulting refresh token in
.env as REFRESH_TOKEN_<USER>. Once that's done, copy the .env file
(or just that one line) over to the NAS.
"""

from __future__ import annotations

import argparse
import os
import warnings
from pathlib import Path

from dotenv import load_dotenv, set_key
from spotipy.oauth2 import CacheHandler, SpotifyOAuth, SpotifyOauthError

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Needs read access to top tracks (our On Repeat stand-in, see
# spotify_client.py), and write access to whichever shared playlist
# this person ends up managing (owner_user_id in config.json).
SCOPES = "user-top-read playlist-read-private playlist-modify-public playlist-modify-private"


class NoOpCacheHandler(CacheHandler):
    """
    We store the refresh token in .env ourselves, so spotipy doesn't
    need to keep its own '.cache' file lying around in the project
    folder - this just tells it not to bother.
    """

    def get_cached_token(self):
        return None

    def save_token_to_cache(self, token_info):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time Spotify OAuth login for one person")
    parser.add_argument(
        "--user",
        required=True,
        help="Short id for this person, e.g. 'you', 'friend', 'friend2' - must match an 'id' in config.json",
    )
    args = parser.parse_args()
    user_id = args.user.strip().lower()

    load_dotenv(ENV_PATH)
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    if not client_id or not client_secret:
        raise SystemExit(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET aren't set in .env yet.\n"
            "Create 1 Spotify app first (see README) and put those two values in .env, "
            "then try again."
        )

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        open_browser=False,
        cache_handler=NoOpCacheHandler(),
    )

    auth_url = auth_manager.get_authorize_url()
    print(f"\nLogging in for '{user_id}':\n")
    print("1. Open the URL below and log in with THIS account (i.e. ", user_id, "'s own account):\n")
    print(f"   {auth_url}\n")
    print("2. Click 'Agree'. Spotify will then redirect you to a URL that looks")
    print("   'unreachable' - that's expected, there's no server running there.")
    print("3. Copy that full URL from your address bar and paste it below.\n")

    redirected_url = input("Pasted URL: ").strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            code = auth_manager.parse_response_code(redirected_url)
            token_info = auth_manager.get_access_token(code, as_dict=True, check_cache=False)
        except SpotifyOauthError as e:
            raise SystemExit(
                f"\nLogin failed ({e}).\n"
                "The code has probably expired (they're only valid briefly) - "
                "run the script again and paste the URL back more quickly."
            )

    refresh_token = token_info["refresh_token"]

    env_key = f"REFRESH_TOKEN_{user_id.upper()}"
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), env_key, refresh_token)

    print(f"\nDone! {env_key} has been saved to {ENV_PATH.name}.")
    print(f'Now add "{user_id}" to config.json under "users" (and to the playlist(s) they should be part of).')


if __name__ == "__main__":
    main()
