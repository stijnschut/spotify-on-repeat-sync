"""
Debug script: lists ALL playlists this account has, with owner info.
Standalone, not part of the actual sync project - just useful for
figuring out playlist IDs or checking ownership when something's
unclear.

Usage:
    python debug_list_playlists.py --user you
"""
import argparse
import os

from dotenv import load_dotenv

from spotify_client import get_client_for_user

parser = argparse.ArgumentParser()
parser.add_argument("--user", required=True)
args = parser.parse_args()

load_dotenv(".env")
client_id = os.environ["SPOTIFY_CLIENT_ID"]
client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
refresh_token = os.environ[f"REFRESH_TOKEN_{args.user.upper()}"]

sp = get_client_for_user(client_id, client_secret, refresh_token)

results = sp.current_user_playlists(limit=50)
items = list(results["items"])
while results.get("next"):
    results = sp.next(results)
    items.extend(results["items"])

print(f"\nFound {len(items)} playlists for '{args.user}':\n")
for p in items:
    owner = (p.get("owner") or {}).get("id", "?")
    print(f"  owner={owner!r:20} name={p['name']!r:35} id={p['id']}")
