"""
SQLite-backed tracking of which tracks currently belong in each shared
playlist, and when each track was last seen in someone's top tracks.

One database file is shared by every playlist defined in config.json.
Rows are scoped by `playlist_name` (the "name" field from config), not
by Spotify's own playlist ID, so the config can be edited freely
without losing history.

This module only handles persistence. The rules about *when* to evict
a track (max_total, max_per_user) live in sync.py, on purpose - this
keeps the database dumb and the business logic in one obvious place.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# How many sync runs to keep in history per playlist (viewing shows fewer).
MAX_HISTORY = 10


class TrackDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL mode lets readers and writers coexist — critical on a NAS
        # where a daily cron job and a manual run might overlap.
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    playlist_name TEXT NOT NULL,
                    track_id      TEXT NOT NULL,
                    source_user   TEXT NOT NULL,
                    added_date    TEXT NOT NULL,
                    last_seen     TEXT NOT NULL,
                    PRIMARY KEY (playlist_name, track_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_history (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_name TEXT NOT NULL,
                    run_date      TEXT NOT NULL,
                    summary       TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS track_names (
                    track_id     TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL
                )
                """
            )

    def track_exists(self, playlist_name: str, track_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tracks WHERE playlist_name = ? AND track_id = ?",
                (playlist_name, track_id),
            ).fetchone()
            return row is not None

    def update_last_seen(self, playlist_name: str, track_id: str, today: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tracks SET last_seen = ? WHERE playlist_name = ? AND track_id = ?",
                (today, playlist_name, track_id),
            )

    def add_track(self, playlist_name: str, track_id: str, source_user: str, today: str) -> None:
        """Insert a brand-new track. Caller must already have checked track_exists()."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tracks (playlist_name, track_id, source_user, added_date, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (playlist_name, track_id, source_user, today, today),
            )

    def remove_track(self, playlist_name: str, track_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM tracks WHERE playlist_name = ? AND track_id = ?",
                (playlist_name, track_id),
            )

    def count_total(self, playlist_name: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM tracks WHERE playlist_name = ?",
                (playlist_name,),
            ).fetchone()
            return row["c"]

    def count_for_user(self, playlist_name: str, source_user: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM tracks WHERE playlist_name = ? AND source_user = ?",
                (playlist_name, source_user),
            ).fetchone()
            return row["c"]

    def get_oldest(
        self, playlist_name: str, before_date: str, source_user: str | None = None
    ) -> sqlite3.Row | None:
        """
        Return the least-recently-seen track that is older than
        `before_date` (i.e. NOT already refreshed today), optionally
        restricted to one user's own tracks.

        Returns None if nothing qualifies - e.g. every candidate track
        was already refreshed today, so there's nothing safe to evict
        without kicking out something that's still "hot".
        """
        query = "SELECT * FROM tracks WHERE playlist_name = ? AND last_seen < ?"
        params: list = [playlist_name, before_date]
        if source_user is not None:
            query += " AND source_user = ?"
            params.append(source_user)
        query += " ORDER BY last_seen ASC, added_date ASC LIMIT 1"
        with self._connect() as conn:
            return conn.execute(query, params).fetchone()

    def get_all_track_ids(self, playlist_name: str) -> list:
        """All track ids currently tracked for a playlist, oldest-added first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT track_id FROM tracks WHERE playlist_name = ? ORDER BY added_date ASC",
                (playlist_name,),
            ).fetchall()
            return [r["track_id"] for r in rows]

    def get_track_sources(
        self, playlist_name: str, track_ids: list[str]
    ) -> dict[str, str]:
        """Return {track_id: source_user} for the given tracks in this playlist."""
        if not track_ids:
            return {}
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in track_ids)
            rows = conn.execute(
                f"SELECT track_id, source_user FROM tracks "
                f"WHERE playlist_name = ? AND track_id IN ({placeholders})",
                [playlist_name] + track_ids,
            ).fetchall()
            return {r["track_id"]: r["source_user"] for r in rows}

    def refresh_tracks(
        self, playlist_name: str, track_ids: list[str], today: str
    ) -> list[str]:
        """
        For each track_id: if the track already exists in this
        playlist, bump its last_seen to today. Return the ids that
        are NOT yet tracked (i.e. candidates for pass 2).

        Done in a single connection + transaction so it's fast even
        with hundreds of tracks from multiple users.
        """
        with self._connect() as conn:
            conn.executemany(
                "UPDATE tracks SET last_seen = ? WHERE playlist_name = ? AND track_id = ?",
                [(today, playlist_name, tid) for tid in track_ids],
            )
            placeholders = ",".join("?" for _ in track_ids)
            existing = set(
                row[0]
                for row in conn.execute(
                    f"SELECT track_id FROM tracks WHERE playlist_name = ? AND track_id IN ({placeholders})",
                    [playlist_name] + track_ids,
                ).fetchall()
            )
        return [tid for tid in track_ids if tid not in existing]

    def save_track_names(self, names: dict[str, str]) -> None:
        """Cache display names for tracks (upsert)."""
        if not names:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO track_names (track_id, display_name) VALUES (?, ?)
                ON CONFLICT(track_id) DO UPDATE SET display_name = excluded.display_name
                """,
                list(names.items()),
            )

    def get_playlist_snapshot(self, playlist_name: str) -> dict[str, list[str]]:
        """Return {source_user: [display names]} for every track currently
        in this playlist, ordered oldest-added first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.source_user, COALESCE(n.display_name, t.track_id) AS name
                FROM tracks t
                LEFT JOIN track_names n ON n.track_id = t.track_id
                WHERE t.playlist_name = ?
                ORDER BY t.added_date ASC, t.track_id ASC
                """,
                (playlist_name,),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["source_user"], []).append(r["name"])
        return result

    def record_sync_run(
        self, playlist_name: str, run_date: str, summary: dict
    ) -> None:
        """Store one sync run's summary and prune history to MAX_HISTORY."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_history (playlist_name, run_date, summary) VALUES (?, ?, ?)",
                (playlist_name, run_date, json.dumps(summary, ensure_ascii=False)),
            )
            conn.execute(
                """
                DELETE FROM sync_history
                WHERE playlist_name = ? AND id NOT IN (
                    SELECT id FROM sync_history
                    WHERE playlist_name = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (playlist_name, playlist_name, MAX_HISTORY),
            )

    def get_sync_history(self, playlist_name: str, limit: int = 3) -> list[dict]:
        """Return the most recent sync summaries for a playlist, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_date, summary FROM sync_history "
                "WHERE playlist_name = ? ORDER BY id DESC LIMIT ?",
                (playlist_name, limit),
            ).fetchall()
        result = []
        for r in rows:
            try:
                summary = json.loads(r["summary"])
            except (json.JSONDecodeError, TypeError):
                summary = {}
            result.append({"run_date": r["run_date"], "summary": summary})
        return result
