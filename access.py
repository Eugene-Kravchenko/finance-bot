"""Доступ второго человека к записям.

Правила:
- по умолчанию записи видит только участница;
- второй человек получает одноразовый код только от неё и отправляет боту /join КОД;
- после этого участница отдельно подтверждает доступ; до подтверждения он ничего не видит;
- она может выключить доступ, скрыть заметки или удалить человека в любой момент;
- проверка выполняется при каждом запросе, кэша нет.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import storage

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_TTL = timedelta(hours=24)


@dataclass
class Target:
    owner_chat_id: int
    role: str  # owner | viewer
    show_private: bool  # заметки, побочные эффекты, ответы о безопасности


def _now() -> str:
    return datetime.now(config.TZ).isoformat()


def is_participant(chat_id: int) -> bool:
    with storage.db() as conn:
        row = conn.execute("SELECT 1 FROM participants WHERE chat_id = ?", (chat_id,)).fetchone()
    return row is not None


def get(owner_chat_id: int):
    with storage.db() as conn:
        return conn.execute(
            "SELECT * FROM sharing WHERE owner_chat_id = ?", (owner_chat_id,)
        ).fetchone()


def _ensure_row(conn, owner_chat_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sharing(owner_chat_id, enabled, share_notes, updated_at) VALUES (?, 0, 0, ?)",
        (owner_chat_id, _now()),
    )


def create_invite(owner_chat_id: int) -> str:
    code = "".join(secrets.choice(ALPHABET) for _ in range(6))
    expires = (datetime.now(config.TZ) + INVITE_TTL).isoformat()
    with storage.db() as conn:
        _ensure_row(conn, owner_chat_id)
        conn.execute(
            "UPDATE sharing SET invite_code = ?, invite_expires_at = ?, updated_at = ? WHERE owner_chat_id = ?",
            (code, expires, _now(), owner_chat_id),
        )
        storage.log_event(conn, owner_chat_id, "share_invite_created", _now())
    return code


def accept_invite(code: str, viewer_chat_id: int, viewer_name: str) -> int | None:
    """Привязывает человека по коду. Доступ остаётся выключенным до подтверждения."""
    code = code.strip().upper()
    if not code:
        return None
    with storage.db() as conn:
        row = conn.execute("SELECT * FROM sharing WHERE invite_code = ?", (code,)).fetchone()
        if not row or row["owner_chat_id"] == viewer_chat_id:
            return None
        if datetime.fromisoformat(row["invite_expires_at"]) < datetime.now(config.TZ):
            return None
        conn.execute(
            """UPDATE sharing SET viewer_chat_id = ?, viewer_name = ?, enabled = 0,
               invite_code = NULL, invite_expires_at = NULL, updated_at = ?
               WHERE owner_chat_id = ?""",
            (viewer_chat_id, viewer_name, _now(), row["owner_chat_id"]),
        )
        storage.log_event(conn, row["owner_chat_id"], "share_viewer_linked", _now(), str(viewer_chat_id))
        return int(row["owner_chat_id"])


def set_enabled(owner_chat_id: int, enabled: bool) -> None:
    with storage.db() as conn:
        _ensure_row(conn, owner_chat_id)
        conn.execute(
            "UPDATE sharing SET enabled = ?, updated_at = ? WHERE owner_chat_id = ? AND viewer_chat_id IS NOT NULL",
            (int(enabled), _now(), owner_chat_id),
        )
        storage.log_event(conn, owner_chat_id, "share_on" if enabled else "share_off", _now())


def set_share_notes(owner_chat_id: int, share: bool) -> None:
    with storage.db() as conn:
        _ensure_row(conn, owner_chat_id)
        conn.execute(
            "UPDATE sharing SET share_notes = ?, updated_at = ? WHERE owner_chat_id = ?",
            (int(share), _now(), owner_chat_id),
        )
        storage.log_event(conn, owner_chat_id, "share_notes_on" if share else "share_notes_off", _now())


def remove_viewer(owner_chat_id: int) -> None:
    with storage.db() as conn:
        conn.execute(
            """UPDATE sharing SET viewer_chat_id = NULL, viewer_name = NULL, enabled = 0,
               invite_code = NULL, invite_expires_at = NULL, updated_at = ?
               WHERE owner_chat_id = ?""",
            (_now(), owner_chat_id),
        )
        storage.log_event(conn, owner_chat_id, "share_viewer_removed", _now())


def resolve_target(requester_chat_id: int) -> Target | None:
    """Чьи записи может смотреть этот чат. None — ничьи."""
    if is_participant(requester_chat_id):
        return Target(requester_chat_id, "owner", True)
    with storage.db() as conn:
        row = conn.execute(
            "SELECT owner_chat_id, share_notes FROM sharing WHERE viewer_chat_id = ? AND enabled = 1",
            (requester_chat_id,),
        ).fetchone()
    if not row:
        return None
    return Target(int(row["owner_chat_id"]), "viewer", bool(row["share_notes"]))
