"""SQLite persistence: round-trips, restart recovery, and failure tolerance."""

import pytest

from app.jobs import JobRegistry
from app.models import JobStatus, Product
from app.storage import MAX_RUNS, Storage

pytestmark = pytest.mark.asyncio


def make_job(job_id="abc123", n=3, with_emails=2) -> JobStatus:
    results = [
        Product(
            rank=i,
            product_name=f"Product {i}",
            tagline=f"Tagline {i}",
            description="A description",
            votes=100 - i,
            comments=i,
            producthunt_url=f"https://www.producthunt.com/products/p{i}",
            website_url="https://www.producthunt.com/r/ABC",
            topics=["AI", "Dev Tools"],
            launch_date="2026-08-22",
            company_name=f"Co {i}",
            domain=f"p{i}.io" if i <= with_emails else "",
            email=f"hello@p{i}.io" if i <= with_emails else "",
            email_verified=i <= with_emails,
            enrichment_status="found" if i <= with_emails else "no_email",
        )
        for i in range(1, n + 1)
    ]
    return JobStatus(
        job_id=job_id,
        state="done",
        message=f"Done — {with_emails} of {n} launches have an email.",
        total=n,
        enriched=n,
        results=results,
        range_from="2026-08-22",
        range_to="2026-08-22",
    )


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "test.db")


class TestRoundTrip:
    async def test_save_then_load_preserves_every_field(self, storage):
        await storage.init()
        await storage.save_run(make_job(), "daily")

        loaded = await storage.load_last_run()
        assert loaded is not None
        assert loaded.job_id == "abc123"
        assert loaded.state == "done"
        assert loaded.total == 3
        assert loaded.range_from == "2026-08-22"
        assert len(loaded.results) == 3

        first = loaded.results[0]
        assert first.product_name == "Product 1"
        assert first.email == "hello@p1.io"
        assert first.email_verified is True
        assert first.topics == ["AI", "Dev Tools"]
        assert first.enrichment_status == "found"

    async def test_unverified_and_empty_states_survive(self, storage):
        await storage.init()
        await storage.save_run(make_job(n=3, with_emails=1), "daily")
        loaded = await storage.load_last_run()

        assert loaded.results[0].email_verified is True
        assert loaded.results[1].email == ""
        assert loaded.results[1].enrichment_status == "no_email"

    async def test_results_come_back_in_rank_order(self, storage):
        await storage.init()
        await storage.save_run(make_job(n=5), "daily")
        loaded = await storage.load_last_run()
        assert [p.rank for p in loaded.results] == [1, 2, 3, 4, 5]

    async def test_load_specific_run_by_id(self, storage):
        await storage.init()
        await storage.save_run(make_job("first"), "daily")
        await storage.save_run(make_job("second"), "weekly")

        assert (await storage.load_run("first")).job_id == "first"
        assert await storage.load_run("nope") is None

    async def test_empty_database_returns_none(self, storage):
        await storage.init()
        assert await storage.load_last_run() is None

    async def test_resaving_the_same_job_replaces_its_results(self, storage):
        await storage.init()
        await storage.save_run(make_job("j1", n=5), "daily")
        await storage.save_run(make_job("j1", n=2), "daily")

        loaded = await storage.load_run("j1")
        assert len(loaded.results) == 2

    async def test_zero_result_run_is_still_recorded(self, storage):
        await storage.init()
        job = make_job(n=0)
        job.message = "No launches found in this window."
        await storage.save_run(job, "custom")

        loaded = await storage.load_last_run()
        assert loaded.results == []
        assert "No launches" in loaded.message


class TestHistoryAndTrimming:
    async def test_history_is_newest_first_with_counts(self, storage):
        await storage.init()
        await storage.save_run(make_job("old", n=3, with_emails=1), "daily")
        await storage.save_run(make_job("new", n=4, with_emails=4), "weekly")

        hist = await storage.history()
        assert [h["job_id"] for h in hist][0] == "new"
        assert hist[0]["with_email"] == 4
        assert hist[0]["timeframe"] == "weekly"

    async def test_old_runs_are_trimmed_at_the_cap(self, storage):
        await storage.init()
        for i in range(MAX_RUNS + 5):
            await storage.save_run(make_job(f"job{i:03d}", n=1), "daily")

        hist = await storage.history(limit=50)
        assert len(hist) <= MAX_RUNS

    async def test_trimming_removes_orphaned_results(self, storage):
        await storage.init()
        for i in range(MAX_RUNS + 3):
            await storage.save_run(make_job(f"job{i:03d}", n=1), "daily")

        # The very first run should be gone entirely, rows included.
        assert await storage.load_run("job000") is None


class TestRestartRecovery:
    async def test_registry_rehydrates_the_last_run(self, tmp_path):
        db = tmp_path / "test.db"

        before = JobRegistry()
        await before.attach_storage(Storage(db))
        await before._finish(make_job("survivor", n=4), "daily")

        # A fresh registry stands in for a restarted process.
        after = JobRegistry()
        await after.attach_storage(Storage(db))

        assert after.last_completed is not None
        assert after.last_completed.job_id == "survivor"
        assert len(after.last_completed.results) == 4

    async def test_restored_run_is_pollable_by_id(self, tmp_path):
        db = tmp_path / "test.db"

        before = JobRegistry()
        await before.attach_storage(Storage(db))
        await before._finish(make_job("pollme"), "daily")

        after = JobRegistry()
        await after.attach_storage(Storage(db))

        job = await after.get_or_load("pollme")
        assert job is not None and job.state == "done"

    async def test_get_or_load_returns_none_for_unknown_id(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        assert await reg.get_or_load("ghost") is None

    async def test_registry_works_without_storage_attached(self):
        reg = JobRegistry()
        assert await reg.get_or_load("anything") is None


class TestFailureTolerance:
    async def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        """A finished run must never fail because persistence broke."""
        import sqlite3

        s = Storage(tmp_path / "t.db")
        await s.init()

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk is full")

        monkeypatch.setattr(s, "_save_sync", boom)
        await s.save_run(make_job(), "daily")  # must not raise

    async def test_load_failure_returns_none(self, tmp_path, monkeypatch):
        import sqlite3

        s = Storage(tmp_path / "t.db")
        await s.init()

        def boom(*a, **k):
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr(s, "_load_sync", boom)
        assert await s.load_last_run() is None

    async def test_unreadable_row_is_skipped_not_fatal(self, tmp_path):
        s = Storage(tmp_path / "t.db")
        await s.init()
        await s.save_run(make_job(n=2), "daily")

        # Corrupt one stored result the way a schema change might.
        import sqlite3

        with sqlite3.connect(s.db_path) as conn:
            conn.execute(
                "UPDATE results SET payload = ? WHERE rank = 1", ("{not json",)
            )

        loaded = await s.load_last_run()
        assert len(loaded.results) == 1  # the good row still comes back

    async def test_init_creates_missing_parent_directory(self, tmp_path):
        s = Storage(tmp_path / "nested" / "deeper" / "t.db")
        await s.init()
        assert s.db_path.exists()
