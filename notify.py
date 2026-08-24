"""Discord webhook notifications — send sync "patch notes" as a Discord embed.

Kept separate from spotify_client.py and sync.py so notification logic is
self-contained. The webhook URLs live in .env (they're semi-secret), keyed
per playlist as DISCORD_WEBHOOK_<NAME> — see webhook_env_key() in sync.py.
"""

from __future__ import annotations

import requests

# Spotify green.
EMBED_COLOR = 0x1DB954

# How many songs to list per user (added) and in the removed section before
# summarising with "... and N more". This keeps big first-sync / DB-wipe
# deltas readable instead of dumping the entire playlist.
MAX_PER_USER = 10
MAX_REMOVED = 20

# Discord caps embed field values at 1024 chars; leave a little headroom.
MAX_FIELD_CHARS = 1000


def _format_songs(songs: list[str], max_items: int) -> str:
    """Join up to `max_items` songs, appending an italic '... and N more'."""
    lines: list[str] = []
    char_count = 0
    for song in songs[:max_items]:
        if char_count + len(song) + 1 > MAX_FIELD_CHARS:
            break
        lines.append(song)
        char_count += len(song) + 1

    text = "\n".join(lines)
    hidden = len(songs) - len(lines)
    if hidden > 0:
        text += f"\n*… and {hidden} more*"
    return text


def send_patch_notes(
    webhook_url: str,
    playlist_name: str,
    added: dict[str, list[str]],
    removed: list[str],
    test: bool = False,
) -> None:
    """Send sync patch notes for one playlist.

    `added` maps a user's display name to their newly-added songs, e.g.
    {"You": ["Kendrick Lamar - HUMBLE.", ...]}. `removed` is a flat list
    of "Artist - Title" strings. Set `test=True` to mark the message
    as a test.
    """
    fields: list[dict] = []

    # One field per user so the embed scales to any group size without
    # running into Discord's 1024-char-per-field limit.
    for user, songs in added.items():
        fields.append(
            {
                "name": f"🟢 {user}",
                "value": _format_songs(songs, MAX_PER_USER),
                "inline": False,
            }
        )

    if removed:
        fields.append(
            {
                "name": "🔴 Removed",
                "value": _format_songs(removed, MAX_REMOVED),
                "inline": False,
            }
        )

    total_added = sum(len(songs) for songs in added.values())
    total_removed = len(removed)

    summary: list[str] = []
    if total_added:
        summary.append(f"+{total_added} added")
    if total_removed:
        summary.append(f"-{total_removed} removed")

    title = f"Playlist: {playlist_name}"
    description = "Sync update · " + " · ".join(summary)
    if test:
        title = f"🧪 TEST — {title}"
        description = "Test message (no real changes were made)"

    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": EMBED_COLOR,
                "fields": fields,
            }
        ]
    }

    response = requests.post(webhook_url, json=payload, timeout=15)
    response.raise_for_status()


def send_test(webhook_url: str, playlist_name: str) -> None:
    """Send a sample patch note to verify a webhook works."""
    send_patch_notes(
        webhook_url,
        playlist_name,
        added={
            "You": [
                "Kendrick Lamar - HUMBLE.",
                "Tame Impala - The Less I Know The Better",
            ],
            "Friend": [
                "Drake - God's Plan",
            ],
        },
        removed=[
            "Post Malone - Circles",
            "The Weeknd - Blinding Lights",
        ],
        test=True,
    )
