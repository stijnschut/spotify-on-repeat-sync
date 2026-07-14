"""Interactive menu-driven CLI for Spotify On Repeat Sync."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from database import TrackDatabase
from sync import load_config, setup_logging

BASE_DIR = Path(__file__).resolve().parent
console = Console()
_err = Console(stderr=True)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _print_header(title: str) -> None:
    console.print()
    console.print(Panel(f"[bold cyan]{title}[/]", box=box.ROUNDED))
    console.print()


def _print_error(msg: str) -> None:
    _err.print(f"[bold red]Error:[/] {msg}")


def _print_success(msg: str) -> None:
    console.print(f"[bold green]✓[/] {msg}")


def _print_warning(msg: str) -> None:
    console.print(f"[bold yellow]⚠[/] {msg}")


def _print_info(msg: str) -> None:
    console.print(f"[bold blue]ℹ[/] {msg}")


def _wait_for_enter() -> None:
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")
    console.print()


def _load_config_safe() -> dict | None:
    """Load config.json, returning None with a printed error on failure."""
    path = BASE_DIR / "config.json"
    if not path.exists():
        _print_error(f"config.json not found at {path}")
        _print_info("Copy config.example.json to config.json first")
        return None
    try:
        return load_config(path)
    except (json.JSONDecodeError, ValueError) as e:
        _print_error(f"Invalid config.json: {e}")
        return None


def _save_config(config: dict) -> bool:
    """Save config.json, returning True on success."""
    path = BASE_DIR / "config.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        return True
    except OSError as e:
        _print_error(f"Could not save config.json: {e}")
        return False


# ─── Menu ───────────────────────────────────────────────────────────────────

def _show_home_menu(version: str = "") -> int:
    """Display the home menu and return the user's choice."""
    console.clear()
    console.print()
    console.print(
        Panel.fit(
            f"[bold cyan]Spotify On Repeat Sync[/]\n"
            f"[dim]Shared playlists from everyone's top tracks[/]",
            box=box.DOUBLE_EDGE,
        )
    )
    console.print()

    menu_items = [
        ("1", "Sync playlists (preview or apply)"),
        ("2", "Add a user (run Spotify OAuth login)"),
        ("3", "View playlist status"),
        ("4", "Manage users & playlists (edit config)"),
        ("0", "Exit"),
    ]

    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("Key", style="bold cyan", width=3)
    table.add_column("Description")
    for key, desc in menu_items:
        table.add_row(key, desc)

    console.print(table)
    console.print()

    choice = Prompt.ask(
        "[bold]Select an option[/]",
        choices=[str(i) for i in range(5)],
        default="0",
    )
    return int(choice)


# ─── Menu Handlers ──────────────────────────────────────────────────────────

def _select_playlists_and_sync() -> None:
    """Let the user pick which playlists to sync, then ask dry-run or live."""
    config = _load_config_safe()
    if not config:
        _wait_for_enter()
        return

    names = [pl["name"] for pl in config["playlists"]]
    if not names:
        _print_warning("No playlists in config.json")
        _wait_for_enter()
        return

    # ── Playlist picker ──
    selected: list[str] = []
    while True:
        console.clear()
        _print_header("Select playlists")

        table = Table(box=box.SQUARE)
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Playlist", style="bold")
        table.add_column("Selected", width=4)
        for i, name in enumerate(names):
            mark = "[green]✓[/]" if name in selected else "[dim]·[/]"
            table.add_row(str(i + 1), name, mark)

        console.print(table)
        console.print()
        console.print(
            "[dim]Select by number (e.g. 3), range (1-3), or comma-separated (1,3).\n"
            "Type [bold]a[/] for all, [bold]done[/] to continue.[/]"
        )
        console.print()

        choice = Prompt.ask("[bold]Your selection[/]", default="done").strip().lower()

        if choice == "done":
            if not selected:
                _print_warning("Nothing selected")
                _wait_for_enter()
            else:
                break
        elif choice == "a":
            selected = list(names)
            _print_success(f"All {len(selected)} playlists selected")
            _wait_for_enter()
        else:
            indices: list[int] = []
            for part in choice.split(","):
                part = part.strip()
                if "-" in part:
                    try:
                        lo, hi = part.split("-", 1)
                        indices.extend(range(int(lo) - 1, int(hi)))
                    except ValueError:
                        continue
                else:
                    try:
                        indices.append(int(part) - 1)
                    except ValueError:
                        continue
            for idx in indices:
                if 0 <= idx < len(names) and names[idx] not in selected:
                    selected.append(names[idx])
            _print_success(f"{len(selected)} playlist(s) selected")
            _wait_for_enter()

    # ── Dry-run or live? ──
    console.clear()
    _print_header("Sync")
    console.print(f"[bold]Playlists:[/] {', '.join(selected)}")
    console.print()

    dry_run = not Confirm.ask(
        "[bold yellow]Apply changes to the real Spotify playlist(s)?[/]",
        default=False,
    )
    if dry_run:
        _print_info("Dry-run: no changes will be made")
    console.print()

    # Build command
    cmd = [sys.executable, str(BASE_DIR / "sync.py")]
    if dry_run:
        cmd.append("--dry-run")
    for name in selected:
        cmd.extend(["--playlist", name])

    console.print(f"[dim]Running: {' '.join(cmd)}[/]")
    console.print()

    result = subprocess.run(cmd)
    if result.returncode == 0:
        _print_success("Done")
    else:
        _print_error(f"Sync exited with code {result.returncode}")
    _wait_for_enter()


def _add_user() -> None:
    """Guide the user through running auth.py for a new person."""
    _print_header("Add a user")
    _print_info(
        "This runs the one-time Spotify OAuth login for a new person.\n"
        "They'll need a browser and must already be added to your\n"
        "Spotify Developer Dashboard under User Management."
    )
    console.print()

    user_id = Prompt.ask("[bold]User ID[/] (short name, e.g. 'friend2')").strip().lower()
    if not user_id:
        _print_warning("No user ID entered, skipping")
        _wait_for_enter()
        return

    cmd = [sys.executable, str(BASE_DIR / "auth.py"), "--user", user_id]
    console.print(f"\n[dim]Running: {' '.join(cmd)}[/]\n")

    result = subprocess.run(cmd)
    if result.returncode == 0:
        _print_success(f"'{user_id}' logged in. Now add them to a playlist in the config.")
    else:
        _print_error("Login failed — check the output above.")

    _wait_for_enter()


def _view_status() -> None:
    """Show DB stats and playlist contents."""
    config = _load_config_safe()
    if not config:
        _wait_for_enter()
        return

    db_path = BASE_DIR / "spotify_sync.db"
    db = TrackDatabase(db_path)

    _print_header("Playlist status")

    for pl in config["playlists"]:
        name = pl["name"]
        total = db.count_total(name)
        max_t = pl.get("max_total", "?")

        table = Table(title=f"[bold cyan]{name}[/] ({total}/{max_t} tracks)", box=box.SIMPLE)
        table.add_column("User", style="bold")
        table.add_column("Tracks", justify="right")
        table.add_column("Cap", justify="right")

        for user in pl.get("members", []):
            count = db.count_for_user(name, user)
            cap = pl.get("max_per_user", "?")
            table.add_row(
                f"[green]{user}[/]" if count > 0 else f"[dim]{user}[/]",
                str(count),
                str(cap),
            )

        console.print(table)
        console.print()

    _wait_for_enter()


def _manage_config() -> None:
    """Submenu for editing config.json interactively."""
    while True:
        config = _load_config_safe()
        if not config:
            _wait_for_enter()
            return

        _print_header("Manage users & playlists")

        # ── Users ──
        user_table = Table(title="[bold cyan]Users[/]", box=box.SIMPLE)
        user_table.add_column("#", style="bold cyan", width=3)
        user_table.add_column("ID", style="bold")
        user_table.add_column("Display name")
        user_table.add_column("Track limit", justify="right")
        for i, u in enumerate(config["users"]):
            user_table.add_row(
                str(i + 1), u["id"], u.get("display_name", ""),
                str(u.get("top_tracks_limit", 30)),
            )
        console.print(user_table)
        console.print()

        # ── Playlists ──
        pl_table = Table(title="[bold cyan]Playlists[/]", box=box.SIMPLE)
        pl_table.add_column("#", style="bold cyan", width=3)
        pl_table.add_column("Name", style="bold")
        pl_table.add_column("Members")
        pl_table.add_column("Total / Per-user", justify="right")
        pl_offset = len(config["users"])  # playlist numbers start after users
        for i, pl in enumerate(config["playlists"]):
            pl_table.add_row(
                str(pl_offset + i + 1),
                pl["name"],
                ", ".join(pl.get("members", [])),
                f"{pl.get('max_total', '?')} / {pl.get('max_per_user', '?')}",
            )
        console.print(pl_table)
        console.print()

        console.print("[cyan]A[/] Add user   [cyan]D[/] Delete user")
        console.print("[cyan]P[/] Add playlist   [cyan]R[/] Remove playlist")
        console.print("[cyan]0[/] Back to main menu")
        console.print()

        choice = Prompt.ask("[bold]Select[/]", choices=["A", "D", "P", "R", "0"], default="0").upper()

        if choice == "0":
            return
        elif choice == "A":
            _add_user_to_config(config)
        elif choice == "D":
            _delete_user_from_config(config)
        elif choice == "P":
            _add_playlist_to_config(config)
        elif choice == "R":
            _delete_playlist_from_config(config)


def _add_user_to_config(config: dict) -> None:
    """Interactively add a user to config.json."""
    _print_header("Add user to config")
    uid = Prompt.ask("[bold]User ID[/] (short name)").strip().lower()
    if not uid:
        return
    display = Prompt.ask("[bold]Display name[/]", default=uid.title()).strip()
    config["users"].append({
        "id": uid,
        "display_name": display,
        "top_tracks_limit": 50,
    })
    if _save_config(config):
        _print_success(f"Added '{uid}'")


def _delete_user_from_config(config: dict) -> None:
    """Interactively remove a user from config.json."""
    _print_header("Remove user")
    ids = [u["id"] for u in config["users"]]
    for i, uid in enumerate(ids):
        console.print(f"  [cyan]{i + 1}[/] {uid}")
    console.print()
    try:
        idx = IntPrompt.ask("[bold]Number to remove[/]", choices=[str(i + 1) for i in range(len(ids))]) - 1
    except (ValueError, IndexError):
        return
    removed = config["users"].pop(idx)
    # Also remove from all playlist members
    for pl in config["playlists"]:
        if removed["id"] in pl.get("members", []):
            pl["members"].remove(removed["id"])
    if _save_config(config):
        _print_success(f"Removed '{removed['id']}' from config")


def _add_playlist_to_config(config: dict) -> None:
    """Interactively add a playlist to config.json."""
    _print_header("Add playlist")
    name = Prompt.ask("[bold]Playlist name[/] (e.g. 'friend_group')").strip().lower()
    if not name:
        return
    owner = Prompt.ask("[bold]Owner user ID[/]", default="stijn").strip().lower()
    _print_info("Enter member IDs (the short names), one per line. Empty line to finish.")
    members: list[str] = []
    while True:
        m = Prompt.ask("[bold]Member[/]", default="").strip().lower()
        if not m:
            if members:
                break
            _print_warning("Need at least one member")
            continue
        members.append(m)
    max_total = IntPrompt.ask("[bold]Max total tracks[/]", default=100)
    max_per_user = IntPrompt.ask("[bold]Max per user[/]", default=50)
    config["playlists"].append({
        "name": name,
        "owner_user_id": owner,
        "members": members,
        "max_total": max_total,
        "max_per_user": max_per_user,
    })
    if _save_config(config):
        _print_success(f"Added playlist '{name}'")
        _print_info(f"Don't forget: add PLAYLIST_ID_{name.upper().replace('-', '_').replace(' ', '_')} to .env!")


def _delete_playlist_from_config(config: dict) -> None:
    """Interactively remove a playlist from config.json."""
    _print_header("Remove playlist")
    names = [pl["name"] for pl in config["playlists"]]
    for i, name in enumerate(names):
        console.print(f"  [cyan]{i + 1}[/] {name}")
    console.print()
    try:
        idx = IntPrompt.ask("[bold]Number to remove[/]", choices=[str(i + 1) for i in range(len(names))]) - 1
    except (ValueError, IndexError):
        return
    removed = config["playlists"].pop(idx)
    if _save_config(config):
        _print_success(f"Removed playlist '{removed['name']}'")


# ─── Main Loop ──────────────────────────────────────────────────────────────

def main() -> None:
    """Run the interactive menu."""
    if not sys.stdout.isatty():
        _err.print("Interactive mode requires a terminal. Use 'python sync.py' for non-interactive sync.")
        sys.exit(1)

    while True:
        try:
            choice = _show_home_menu()
            if choice == 0:
                console.print("\n[bold cyan]Goodbye![/] 🎵\n")
                break
            elif choice == 1:
                _select_playlists_and_sync()
            elif choice == 2:
                _add_user()
            elif choice == 3:
                _view_status()
            elif choice == 4:
                _manage_config()
        except KeyboardInterrupt:
            console.print("\n\n[bold yellow]Interrupted. Exiting.[/]")
            break
        except Exception:
            console.print_exception()
            _wait_for_enter()


if __name__ == "__main__":
    main()
