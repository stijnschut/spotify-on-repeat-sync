"""
Thin wrapper around the Spotify Web API (via spotipy) for the sync
script: turning a stored refresh token into a usable client, reading
someone's current "top tracks" (our stand-in for On Repeat - see
note below), and reading/writing playlist tracks.

IMPORTANT: as of Spotify's February 2026 Web API changes, apps can no
longer read the contents of ANY Spotify-owned/algorithmic playlist
(On Repeat, Discover Weekly, Release Radar, Daily Mixes, etc.) - this
returns a 404/403 for everyone, including the account owner, and it's
a deliberate permanent restriction, not a bug. So instead we use
GET /me/top/tracks with time_range="short_term" (roughly the last ~4
weeks of listening) - Spotify's own personalization endpoint, not a
playlist, and NOT subject to that restriction. It's the closest
available stand-in for "songs you're currently into".
"""

from __future__ import annotations

import requests
import spotipy

TOKEN_URL = "https://accounts.spotify.com/api/token"

# Spotify's documented max for one call to /me/top/tracks.
MAX_TOP_TRACKS_LIMIT = 50


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a long-lived refresh token for a short-lived access token."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def get_client_for_user(
    client_id: str, client_secret: str, refresh_token: str
) -> spotipy.Spotify:
    """Build a ready-to-use Spotify client authenticated as one person."""
    access_token = get_access_token(client_id, client_secret, refresh_token)
    return spotipy.Spotify(auth=access_token)


def get_top_track_ids(
    sp: spotipy.Spotify, time_range: str = "short_term", limit: int = 30
) -> list[str]:
    """
    The current user's top tracks over `time_range`
    ("short_term" ~4 weeks, "medium_term" ~6 months, "long_term" ~years).
    "short_term" is the closest match to what On Repeat used to represent.
    """
    limit = min(limit, MAX_TOP_TRACKS_LIMIT)
    results = sp.current_user_top_tracks(time_range=time_range, limit=limit)
    return [item["id"] for item in results["items"] if item and item.get("id")]


def get_playlist_track_ids(sp: spotipy.Spotify, playlist_id: str) -> list[str]:
    """Track IDs in a playlist, in playlist order. Skips local files / unavailable tracks."""
    track_ids: list[str] = []
    results = sp.playlist_items(
        playlist_id,
        fields="items(item(id)),next",
        additional_types=["track"],
    )
    while True:
        for item in results["items"]:
            track = item.get("item")
            if track and track.get("id"):
                track_ids.append(track["id"])
        if not results.get("next"):
            break
        results = sp.next(results)
    return track_ids


def add_tracks(sp: spotipy.Spotify, playlist_id: str, track_ids: list[str]) -> None:
    """Add tracks to a playlist, batched by 100 (Spotify's limit per request)."""
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        sp.playlist_add_items(playlist_id, [f"spotify:track:{t}" for t in batch])


def remove_tracks(sp: spotipy.Spotify, playlist_id: str, track_ids: list[str]) -> None:
    """Remove tracks from a playlist, batched by 100."""
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        sp.playlist_remove_all_occurrences_of_items(
            playlist_id, [f"spotify:track:{t}" for t in batch]
        )
