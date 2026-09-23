"""Persistent evilazp session storage.

The session file stores Azure DevOps connection settings so the interactive
shell can reconnect without asking for organization and PAT on every launch.
The file is created with user-only permissions because PAT values are secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SESSION_DIR = Path.home() / ".evilazp"
SESSION_FILE = SESSION_DIR / "sessions.json"


@dataclass(frozen=True)
class SessionRecord:
    """A saved Azure DevOps shell connection."""

    name: str
    organization: str
    pat: str
    project: str | None = None
    pipeline: str | None = None
    pool: str | None = None
    sp_tenant_id: str | None = None
    sp_client_id: str | None = None
    sp_client_secret: str | None = None
    created_at: str = ""
    last_used_at: str = ""


def default_session_path() -> Path:
    return Path(os.environ.get("EVILAZP_SESSIONS", SESSION_FILE)).expanduser()


def load_sessions(path: Path | None = None) -> list[SessionRecord]:
    session_path = path or default_session_path()
    if not session_path.exists():
        return []
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = data.get("sessions", data if isinstance(data, list) else [])
    sessions: list[SessionRecord] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        if not item.get("name") or not item.get("organization") or not item.get("pat"):
            continue
        sessions.append(
            SessionRecord(
                name=str(item["name"]),
                organization=str(item["organization"]),
                pat=str(item["pat"]),
                project=optional_str(item.get("project")),
                pipeline=optional_str(item.get("pipeline")),
                pool=optional_str(item.get("pool")),
                sp_tenant_id=optional_str(item.get("sp_tenant_id")),
                sp_client_id=optional_str(item.get("sp_client_id")),
                sp_client_secret=optional_str(item.get("sp_client_secret")),
                created_at=str(item.get("created_at") or ""),
                last_used_at=str(item.get("last_used_at") or ""),
            )
        )
    return sessions


def save_session(record: SessionRecord, path: Path | None = None) -> None:
    session_path = path or default_session_path()
    sessions = [item for item in load_sessions(session_path) if item.name != record.name]
    now = utc_now()
    created_at = record.created_at or now
    record = SessionRecord(
        name=record.name,
        organization=record.organization,
        pat=record.pat,
        project=record.project,
        pipeline=record.pipeline,
        pool=record.pool,
        sp_tenant_id=record.sp_tenant_id,
        sp_client_id=record.sp_client_id,
        sp_client_secret=record.sp_client_secret,
        created_at=created_at,
        last_used_at=now,
    )
    sessions.append(record)
    write_sessions(sorted(sessions, key=lambda item: item.name.lower()), session_path)


def mark_session_used(name: str, path: Path | None = None) -> SessionRecord | None:
    session_path = path or default_session_path()
    sessions = load_sessions(session_path)
    updated: list[SessionRecord] = []
    selected: SessionRecord | None = None
    for item in sessions:
        if item.name == name:
            item = SessionRecord(
                name=item.name,
                organization=item.organization,
                pat=item.pat,
                project=item.project,
                pipeline=item.pipeline,
                pool=item.pool,
                sp_tenant_id=item.sp_tenant_id,
                sp_client_id=item.sp_client_id,
                sp_client_secret=item.sp_client_secret,
                created_at=item.created_at,
                last_used_at=utc_now(),
            )
            selected = item
        updated.append(item)
    if selected:
        write_sessions(updated, session_path)
    return selected


def delete_session(name: str, path: Path | None = None) -> bool:
    session_path = path or default_session_path()
    sessions = load_sessions(session_path)
    remaining = [item for item in sessions if item.name != name]
    if len(remaining) == len(sessions):
        return False
    write_sessions(remaining, session_path)
    return True


def write_sessions(sessions: list[SessionRecord], path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {"sessions": [asdict(item) for item in sessions]}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def print_sessions_table(sessions: list[SessionRecord]) -> None:
    if not sessions:
        print("No saved sessions.")
        return
    rows = [
        (
            str(index),
            item.name,
            item.organization,
            item.project or "-",
            item.pool or "-",
            item.last_used_at or "-",
        )
        for index, item in enumerate(sessions, start=1)
    ]
    headers = ("#", "Name", "Organization", "Project", "Pool", "Last used")
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def find_session(target: str, sessions: list[SessionRecord]) -> SessionRecord | None:
    if target.isdigit():
        index = int(target)
        if 1 <= index <= len(sessions):
            return sessions[index - 1]
        return None
    matches = [item for item in sessions if item.name == target]
    return matches[0] if len(matches) == 1 else None


def masked_pat(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def masked_secret(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}...{value[-2:]}"


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
