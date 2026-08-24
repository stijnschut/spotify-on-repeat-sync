"""Discord webhook notifications — send sync "patch notes" as a Discord embed.

Kept separate from spotify_client.py and sync.py so notification logic is
self-contained. The webhook URLs live in .env (they're semi-secret), keyed
per playlist as DISCORD_WEBHOOK_<NAME> — see webhook_env_key() in sync.py.
"""

from __future__ import annotations

import requests

# Spotify green.
EMBED_COLOR = 0x1DB954

# Discord caps embed field values at 1024 chars; keep messages readable.
MAX_FIELD_CHARS = 1000
MAX_ITEMS = 25


def _format_list(items: list[str]) -> str:
    """Join items into one field value, truncating with '... and N more'."""
    lines: list[str] = []
    char_count = 0
    for item in items:
        if len(lines) >= MAX_ITEMS:
            break
        if char_count + len(item) + 1 > MAX_FIELD_CHARS:
            break
        lines.append(item)
        char_count += len(item) + 1

    shown = "\n".join(lines)
    hidden = len(items) - len(lines)
    if hidden > 0:
        shown += f"\n… and {hidden} more"
    return shown


def send_patch_notes(
    webhook_url: str,
    playlist_name: str,
    added: list[str],
    removed: list[str],
) -> None:
    """Send sync patch notes for one playlist.

    `added` and `removed` are pre-formatted display strings, e.g.
    added:   "Kendrick Lamar - HUMBLE. (by Stijn)"
    removed: "Drake - God's Plan"
    """
    fields: list[dict] = []
    if added:
        fields.append(
            {
                "name": f"🟢 Songs added (+{len(added)})",
                "value": _format_list(added),
                "inline": False,
            }
        )
    if removed:
        fields.append(
            {
                "name": f"🔴 Songs removed (-{len(removed)})",
                "value": _format_list(removed),
                "inline": False,
            }
        )

    payload = {
        "embeds": [
            {
                "title": f"Playlist: {playlist_name}",
                "description": "Sync update",
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
        added=[
            "Kendrick Lamar - HUMBLE. (by You)",
            "Tame Impala - The Less I Know The Better (by Friend)",
        ],
        removed=[
            "Drake - God's Plan",
            "Post Malone - Circles",
        ],
    )
