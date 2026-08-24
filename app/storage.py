"""SQLite persistence so completed runs survive a server restart.

Only *completed* runs are written — an in-flight job lives in memory until it
finishes, then lands here in a single transaction. That keeps writes cheap and
means a crash mid-run leaves no half-populated row behind.

Uses stdlib sqlite3 rather than an async driver: each call is short and runs
off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import DEFAULT_LEAD_STATUS, JobStatus, Lead, LeadEvent, Product

log = logging.getLogger("huntbox.storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    job_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    timeframe   TEXT NOT NULL DEFAULT '',
    range_from  TEXT NOT NULL DEFAULT '',
    range_to    TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL DEFAULT 0,
    with_email  INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS results (
    job_id      TEXT NOT NULL REFERENCES runs(job_id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (job_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

-- Outreach state, keyed by company domain. Deliberately NOT tied to a run:
-- a lead marked "Contacted" today must still read "Contacted" when the same
-- domain resurfaces in next week's list, long after that run was trimmed.
CREATE TABLE IF NOT EXISTS leads (
    domain      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'Not contacted',
    note        TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);

-- Append-only history. `leads` holds current state for fast lookup; this
-- holds how it got there, so a last-writer-wins overwrite is still traceable.
CREATE TABLE IF NOT EXISTS lead_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    note_changed INTEGER NOT NULL DEFAULT 0,
    actor       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_domain ON lead_events(domain, id DESC);
"""

# Two runs can land inside the same clock tick, so created_at alone is not a
# stable sort. rowid breaks the tie in insertion order (INSERT OR REPLACE
# assigns a fresh one, which is what we want for a re-saved run).
NEWEST_FIRST = "ORDER BY created_at DESC, rowid DESC"

# Keep the file from growing without bound on a long-lived server.
MAX_RUNS = 50


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # -- connection -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # -- sync internals (run off-thread) ----------------------------------

    def _init_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Additive migration for databases created before fetched_at.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            if "fetched_at" not in existing:
                conn.execute("ALTER TABLE runs ADD COLUMN fetched_at TEXT NOT NULL DEFAULT ''")
                log.info("Migrated runs table: added fetched_at")

    def _save_sync(self, job: JobStatus, timeframe: str) -> None:
        with_email = sum(1 for p in job.results if p.email)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (job_id, created_at, timeframe, range_from, range_to,
                    message, total, with_email, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    job.job_id,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    timeframe,
                    job.range_from,
                    job.range_to,
                    job.message,
                    len(job.results),
                    with_email,
                    job.fetched_at,
                ),
            )
            conn.execute("DELETE FROM results WHERE job_id = ?", (job.job_id,))
            conn.executemany(
                "INSERT INTO results (job_id, rank, payload) VALUES (?,?,?)",
                [
                    (job.job_id, p.rank, json.dumps(p.model_dump(), ensure_ascii=False))
                    for p in job.results
                ],
            )
            # Trim the oldest runs beyond the cap.
            conn.execute(
                f"""DELETE FROM runs WHERE job_id IN (
                        SELECT job_id FROM runs {NEWEST_FIRST} LIMIT -1 OFFSET ?
                    )""",
                (MAX_RUNS,),
            )

    def _load_sync(self, job_id: str | None) -> JobStatus | None:
        with self._connect() as conn:
            if job_id:
                row = conn.execute("SELECT * FROM runs WHERE job_id = ?", (job_id,)).fetchone()
            else:
                row = conn.execute(f"SELECT * FROM runs {NEWEST_FIRST} LIMIT 1").fetchone()
            if row is None:
                return None

            results = conn.execute(
                "SELECT payload FROM results WHERE job_id = ? ORDER BY rank",
                (row["job_id"],),
            ).fetchall()

        products: list[Product] = []
        for r in results:
            try:
                products.append(Product(**json.loads(r["payload"])))
            except Exception:  # noqa: BLE001 - a schema change shouldn't break startup
                log.warning("Skipping unreadable stored result for run %s", row["job_id"])

        return JobStatus(
            job_id=row["job_id"],
            state="done",
            message=row["message"],
            total=row["total"],
            enriched=row["total"],
            results=products,
            range_from=row["range_from"],
            range_to=row["range_to"],
            fetched_at=row["fetched_at"] if "fetched_at" in row.keys() else "",
        )

    def _history_sync(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT job_id, created_at, timeframe, range_from, range_to,
                           total, with_email, fetched_at
                    FROM runs {NEWEST_FIRST} LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- leads (sync internals) -------------------------------------------

    def _leads_for_sync(self, domains: list[str]) -> dict[str, Lead]:
        if not domains:
            return {}
        out: dict[str, Lead] = {}
        with self._connect() as conn:
            # Chunked to stay under SQLite's variable limit on big runs.
            for i in range(0, len(domains), 400):
                chunk = domains[i : i + 400]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT * FROM leads WHERE domain IN ({placeholders})", chunk
                ).fetchall()
                for r in rows:
                    out[r["domain"]] = Lead(**dict(r))
        return out

    def _upsert_lead_sync(
        self, domain: str, status: str | None, note: str | None, updated_by: str
    ) -> Lead:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM leads WHERE domain = ?", (domain,)
            ).fetchone()

            # A PATCH may carry only a status or only a note; keep the other.
            new_status = status or (existing["status"] if existing else DEFAULT_LEAD_STATUS)
            new_note = note if note is not None else (existing["note"] if existing else "")

            conn.execute(
                """INSERT INTO leads (domain, status, note, updated_by, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                       status=excluded.status, note=excluded.note,
                       updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (domain, new_status, new_note, updated_by, now),
            )

            prev_status = existing["status"] if existing else ""
            prev_note = existing["note"] if existing else ""
            conn.execute(
                """INSERT INTO lead_events
                   (domain, from_status, to_status, note, note_changed, actor, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    domain, prev_status, new_status, new_note,
                    1 if new_note != prev_note else 0, updated_by, now,
                ),
            )
        return Lead(
            domain=domain, status=new_status, note=new_note,
            updated_by=updated_by, updated_at=now,
        )

    def _lead_history_sync(self, domain: str, limit: int) -> list[LeadEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT domain, from_status, to_status, note, note_changed,
                          actor, created_at
                   FROM lead_events WHERE domain = ?
                   ORDER BY id DESC LIMIT ?""",
                (domain, limit),
            ).fetchall()
        return [
            LeadEvent(**{**dict(r), "note_changed": bool(r["note_changed"])})
            for r in rows
        ]

    def _prunable_sync(self, older_than_days: int, statuses: list[str]) -> list[Lead]:
        """Leads eligible for pruning: matching status, stale, and note-free.

        A note is someone's work, so a lead carrying one is never prunable
        regardless of age or status.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM leads
                    WHERE status IN ({placeholders})
                      AND TRIM(note) = ''
                      AND updated_at < ?
                    ORDER BY updated_at""",
                [*statuses, cutoff],
            ).fetchall()
        return [Lead(**dict(r)) for r in rows]

    def _prune_sync(self, domains: list[str]) -> int:
        if not domains:
            return 0
        removed = 0
        with self._connect() as conn:
            for i in range(0, len(domains), 400):
                chunk = domains[i : i + 400]
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(f"DELETE FROM leads WHERE domain IN ({ph})", chunk)
                removed += cur.rowcount
                conn.execute(f"DELETE FROM lead_events WHERE domain IN ({ph})", chunk)
        return removed

    def _delete_lead_sync(self, domain: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM leads WHERE domain = ?", (domain,))
            conn.execute("DELETE FROM lead_events WHERE domain = ?", (domain,))
        return cur.rowcount > 0

    def _all_leads_sync(self) -> list[Lead]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM leads ORDER BY updated_at DESC"
            ).fetchall()
        return [Lead(**dict(r)) for r in rows]

    # -- async surface ----------------------------------------------------

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def save_run(self, job: JobStatus, timeframe: str = "") -> None:
        try:
            await asyncio.to_thread(self._save_sync, job, timeframe)
            log.info("Persisted run %s (%d results)", job.job_id, len(job.results))
        except sqlite3.Error as exc:
            # Persistence is a convenience -- never fail a finished run over it.
            log.error("Could not persist run %s: %s", job.job_id, exc)

    async def load_last_run(self) -> JobStatus | None:
        try:
            return await asyncio.to_thread(self._load_sync, None)
        except sqlite3.Error as exc:
            log.error("Could not read last run: %s", exc)
            return None

    async def load_run(self, job_id: str) -> JobStatus | None:
        try:
            return await asyncio.to_thread(self._load_sync, job_id)
        except sqlite3.Error as exc:
            log.error("Could not read run %s: %s", job_id, exc)
            return None

    async def leads_for(self, domains: list[str]) -> dict[str, Lead]:
        """Stored outreach state for these domains, keyed by domain."""
        try:
            return await asyncio.to_thread(self._leads_for_sync, domains)
        except sqlite3.Error as exc:
            log.error("Could not read leads: %s", exc)
            return {}

    async def upsert_lead(
        self,
        domain: str,
        status: str | None = None,
        note: str | None = None,
        updated_by: str = "",
    ) -> Lead | None:
        try:
            return await asyncio.to_thread(
                self._upsert_lead_sync, domain, status, note, updated_by
            )
        except sqlite3.Error as exc:
            log.error("Could not save lead %s: %s", domain, exc)
            return None

    async def lead_history(self, domain: str, limit: int = 50) -> list[LeadEvent]:
        try:
            return await asyncio.to_thread(self._lead_history_sync, domain, limit)
        except sqlite3.Error as exc:
            log.error("Could not read history for %s: %s", domain, exc)
            return []

    async def prunable_leads(
        self, older_than_days: int, statuses: list[str]
    ) -> list[Lead]:
        try:
            return await asyncio.to_thread(
                self._prunable_sync, older_than_days, statuses
            )
        except sqlite3.Error as exc:
            log.error("Could not compute prunable leads: %s", exc)
            return []

    async def prune_leads(self, domains: list[str]) -> int:
        try:
            removed = await asyncio.to_thread(self._prune_sync, domains)
            log.info("Pruned %d leads", removed)
            return removed
        except sqlite3.Error as exc:
            log.error("Could not prune leads: %s", exc)
            return 0

    async def delete_lead(self, domain: str) -> bool:
        try:
            return await asyncio.to_thread(self._delete_lead_sync, domain)
        except sqlite3.Error as exc:
            log.error("Could not delete lead %s: %s", domain, exc)
            return False

    async def all_leads(self) -> list[Lead]:
        try:
            return await asyncio.to_thread(self._all_leads_sync)
        except sqlite3.Error as exc:
            log.error("Could not list leads: %s", exc)
            return []

    async def history(self, limit: int = 20) -> list[dict]:
        try:
            return await asyncio.to_thread(self._history_sync, limit)
        except sqlite3.Error as exc:
            log.error("Could not read run history: %s", exc)
            return []
