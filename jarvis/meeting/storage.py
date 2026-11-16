"""SQLite kalıcı kayıt katmanı (MeetingRepository).

Tasarım:
  * `meetings` tablosu: toplantının tamamı JSON olarak `payload` sütununda
    (PDF Bölüm 5). Listeleme için başlık/tarih/durum ayrı sütunlarda tutulur.
  * `action_items` tablosu: görevlerin sorgulanabilir kopyası. Devreden açık
    görevler ve hatırlatmalar bu tablodan, toplantı JSON'unu açmadan sorgulanır.
    İki tablo her kayıtta aynı işlem (transaction) içinde güncellenir.
  * WAL günlüğü + synchronous=FULL: Ctrl+C veya elektrik kesintisinde son
    tamamlanmış kayıt korunur.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from .models import ActionItem, ActionStatus, Meeting, MeetingStatus, dt_to_str, now

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_started ON meetings(started_at);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
CREATE TABLE IF NOT EXISTS action_items (
    id           TEXT PRIMARY KEY,
    meeting_id   TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    due_date     TEXT,
    status       TEXT NOT NULL,
    agenda_item  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_actions_due ON action_items(due_date);
"""


class StorageError(RuntimeError):
    """Veritabanı işlemi başarısız olduğunda fırlatılır."""


class MeetingRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ altyapı
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT version FROM schema_info").fetchone()
                if row is None:
                    conn.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise StorageError(
                f"Veritabanı açılamadı veya bozuk: {self.db_path} ({exc}). "
                "Dosyayı yedekleyip yeniden adlandırın; program yeni bir veritabanı oluşturur."
            ) from exc

    # ------------------------------------------------------------------ kayıt
    def save(self, meeting: Meeting) -> None:
        """Toplantıyı ve görevlerini tek bir işlemde kaydeder."""
        payload = json.dumps(meeting.to_dict(), ensure_ascii=False)
        stamp = dt_to_str(now())
        try:
            with self._transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO meetings(id, title, started_at, ended_at, status, is_demo, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title, started_at=excluded.started_at,
                        ended_at=excluded.ended_at, status=excluded.status,
                        is_demo=excluded.is_demo, payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (meeting.id, meeting.title, dt_to_str(meeting.started_at), dt_to_str(meeting.ended_at),
                     meeting.status, int(meeting.is_demo), payload, stamp),
                )
                for action in meeting.action_items:
                    self._upsert_action(conn, action, meeting.id, stamp)
                # Toplantıdan silinen görevleri tablodan da kaldır
                ids = [a.id for a in meeting.action_items]
                if ids:
                    marks = ",".join("?" * len(ids))
                    conn.execute(
                        f"DELETE FROM action_items WHERE meeting_id = ? AND id NOT IN ({marks})",
                        (meeting.id, *ids),
                    )
                else:
                    conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting.id,))
        except sqlite3.Error as exc:
            raise StorageError(f"Toplantı kaydedilemedi: {exc}") from exc

    @staticmethod
    def _upsert_action(conn: sqlite3.Connection, action: ActionItem, meeting_id: str, stamp: str | None) -> None:
        conn.execute(
            """
            INSERT INTO action_items(id, meeting_id, text, owner, due_date, status, agenda_item,
                                     created_at, completed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text=excluded.text, owner=excluded.owner, due_date=excluded.due_date,
                status=excluded.status, agenda_item=excluded.agenda_item,
                completed_at=excluded.completed_at, updated_at=excluded.updated_at
            """,
            (action.id, action.meeting_id or meeting_id, action.text, action.owner,
             action.due_date.isoformat() if action.due_date else None, action.status,
             action.agenda_item, dt_to_str(action.created_at), dt_to_str(action.completed_at), stamp),
        )

    # ------------------------------------------------------------------ okuma
    @staticmethod
    def _row_to_meeting(row: sqlite3.Row | None) -> Meeting | None:
        if row is None:
            return None
        return Meeting.from_dict(json.loads(row["payload"]))

    def get(self, meeting_id: str) -> Meeting | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT payload FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        finally:
            conn.close()
        return self._row_to_meeting(row)

    def get_active(self, include_demo: bool = False) -> Meeting | None:
        """Kapatılmamış (yarım kalan) en son toplantıyı döndürür."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM meetings WHERE status = ? AND (is_demo = 0 OR ?) "
                "ORDER BY started_at DESC LIMIT 1",
                (MeetingStatus.ACTIVE, int(include_demo)),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_meeting(row)

    def get_last_ended(self, exclude_id: str | None = None, include_demo: bool = False) -> Meeting | None:
        """Tamamlanmış en son toplantıyı döndürür (önceki toplantı özeti için)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM meetings WHERE status = ? AND id != ? AND (is_demo = 0 OR ?) "
                "ORDER BY started_at DESC LIMIT 1",
                (MeetingStatus.ENDED, exclude_id or "", int(include_demo)),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_meeting(row)

    def list_meetings(self, limit: int = 50, include_demo: bool = False) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, title, started_at, ended_at, status, is_demo FROM meetings "
                "WHERE (is_demo = 0 OR ?) ORDER BY started_at DESC LIMIT ?",
                (int(include_demo), limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def get_open_actions(self, exclude_meeting_id: str | None = None,
                         include_demo: bool = False) -> list[ActionItem]:
        """Açık görevleri (devreden işler) son tarihe göre sıralı döndürür."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.* FROM action_items a JOIN meetings m ON m.id = a.meeting_id
                WHERE a.status = ? AND a.meeting_id != ? AND (m.is_demo = 0 OR ?)
                ORDER BY a.due_date IS NULL, a.due_date, a.created_at
                """,
                (ActionStatus.OPEN, exclude_meeting_id or "", int(include_demo)),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_action(row) for row in rows]

    def get_actions(self, action_ids: list[str]) -> list[ActionItem]:
        """Kimlikleri verilen görevleri (durumu ne olursa olsun) döndürür."""
        if not action_ids:
            return []
        marks = ",".join("?" * len(action_ids))
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT * FROM action_items WHERE id IN ({marks})", tuple(action_ids)).fetchall()
        finally:
            conn.close()
        by_id = {row["id"]: self._row_to_action(row) for row in rows}
        return [by_id[action_id] for action_id in action_ids if action_id in by_id]

    def get_meeting_headers(self, meeting_ids: list[str]) -> dict[str, dict]:
        """Toplantı başlık ve tarihlerini (JSON açmadan) döndürür: {id: {title, started_at}}."""
        unique = sorted(set(meeting_ids))
        if not unique:
            return {}
        marks = ",".join("?" * len(unique))
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT id, title, started_at FROM meetings WHERE id IN ({marks})",
                                tuple(unique)).fetchall()
        finally:
            conn.close()
        return {row["id"]: {"title": row["title"], "started_at": row["started_at"]} for row in rows}

    def get_actions_due_between(self, start: date, end: date, include_demo: bool = False) -> list[ActionItem]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.* FROM action_items a JOIN meetings m ON m.id = a.meeting_id
                WHERE a.status = ? AND a.due_date IS NOT NULL AND a.due_date BETWEEN ? AND ?
                  AND (m.is_demo = 0 OR ?)
                ORDER BY a.due_date
                """,
                (ActionStatus.OPEN, start.isoformat(), end.isoformat(), int(include_demo)),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_action(row) for row in rows]

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> ActionItem:
        return ActionItem.from_dict({
            "id": row["id"], "text": row["text"], "owner": row["owner"], "due_date": row["due_date"],
            "status": row["status"], "agenda_item": row["agenda_item"], "meeting_id": row["meeting_id"],
            "created_at": row["created_at"], "completed_at": row["completed_at"],
        })

    def update_action_status(self, action_id: str, status: str) -> ActionItem | None:
        """Bir görevin durumunu hem tabloda hem de ait olduğu toplantının JSON'unda günceller."""
        stamp = now()
        try:
            with self._transaction() as conn:
                row = conn.execute("SELECT meeting_id FROM action_items WHERE id = ?", (action_id,)).fetchone()
                if row is None:
                    return None
                meeting_row = conn.execute("SELECT payload FROM meetings WHERE id = ?",
                                           (row["meeting_id"],)).fetchone()
                meeting = Meeting.from_dict(json.loads(meeting_row["payload"]))
                updated: ActionItem | None = None
                for action in meeting.action_items:
                    if action.id == action_id:
                        action.status = status
                        action.completed_at = stamp if status == ActionStatus.DONE else None
                        updated = action
                if updated is None:
                    return None
                conn.execute("UPDATE meetings SET payload = ?, updated_at = ? WHERE id = ?",
                             (json.dumps(meeting.to_dict(), ensure_ascii=False), dt_to_str(stamp), meeting.id))
                self._upsert_action(conn, updated, meeting.id, dt_to_str(stamp))
                return updated
        except sqlite3.Error as exc:
            raise StorageError(f"Görev güncellenemedi: {exc}") from exc
