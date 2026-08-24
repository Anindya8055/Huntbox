"""Audit trail and lead pruning."""

from datetime import datetime, timedelta, timezone

import pytest

from app.storage import Storage

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "admin.db")
    await s.init()
    return s


def backdate(storage, domain, days):
    """Age a lead's updated_at so pruning rules can be exercised."""
    stamp = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    import sqlite3

    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE leads SET updated_at = ? WHERE domain = ?", (stamp, domain))


class TestAuditTrail:
    async def test_first_write_records_an_event(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="sam")
        events = await storage.lead_history("acme.io")

        assert len(events) == 1
        assert events[0].from_status == ""          # nothing before it
        assert events[0].to_status == "Contacted"
        assert events[0].actor == "sam"
        assert events[0].created_at

    async def test_transitions_are_recorded_in_order(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="sam")
        await storage.upsert_lead("acme.io", status="Replied", updated_by="kim")
        await storage.upsert_lead("acme.io", status="Not a fit", updated_by="ada")

        events = await storage.lead_history("acme.io")
        assert [(e.from_status, e.to_status, e.actor) for e in events] == [
            ("Replied", "Not a fit", "ada"),
            ("Contacted", "Replied", "kim"),
            ("", "Contacted", "sam"),
        ]

    async def test_overwrite_by_a_second_person_stays_traceable(self, storage):
        """Last-writer-wins on `leads`, but the log keeps both writers."""
        await storage.upsert_lead("acme.io", status="Replied", updated_by="sam")
        await storage.upsert_lead("acme.io", status="Not a fit", updated_by="kim")

        current = (await storage.leads_for(["acme.io"]))["acme.io"]
        assert current.status == "Not a fit" and current.updated_by == "kim"

        actors = [e.actor for e in await storage.lead_history("acme.io")]
        assert actors == ["kim", "sam"]

    async def test_note_only_edit_is_flagged(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="sam")
        await storage.upsert_lead("acme.io", note="left voicemail", updated_by="sam")

        latest = (await storage.lead_history("acme.io"))[0]
        assert latest.note_changed is True
        assert latest.note == "left voicemail"
        assert latest.from_status == latest.to_status == "Contacted"

    async def test_status_change_without_note_edit_is_not_flagged(self, storage):
        await storage.upsert_lead("acme.io", note="hi", updated_by="sam")
        await storage.upsert_lead("acme.io", status="Replied", updated_by="sam")
        assert (await storage.lead_history("acme.io"))[0].note_changed is False

    async def test_history_is_per_domain(self, storage):
        await storage.upsert_lead("a.io", status="Contacted", updated_by="s")
        await storage.upsert_lead("b.io", status="Replied", updated_by="s")
        assert len(await storage.lead_history("a.io")) == 1

    async def test_unknown_domain_has_empty_history(self, storage):
        assert await storage.lead_history("nobody.io") == []

    async def test_limit_caps_returned_events(self, storage):
        for i in range(8):
            await storage.upsert_lead("acme.io", note=f"n{i}", updated_by="s")
        assert len(await storage.lead_history("acme.io", limit=3)) == 3


class TestPruning:
    async def test_preview_finds_stale_untouched_leads(self, storage):
        await storage.upsert_lead("old.io", status="Not contacted", updated_by="s")
        backdate(storage, "old.io", 120)
        await storage.upsert_lead("new.io", status="Not contacted", updated_by="s")

        eligible = await storage.prunable_leads(90, ["Not contacted"])
        assert [c.domain for c in eligible] == ["old.io"]

    async def test_a_note_protects_a_lead_forever(self, storage):
        await storage.upsert_lead(
            "old.io", status="Not contacted", note="worth a second look", updated_by="s"
        )
        backdate(storage, "old.io", 999)
        assert await storage.prunable_leads(1, ["Not contacted"]) == []

    async def test_only_requested_statuses_are_eligible(self, storage):
        for d, st in [("a.io", "Not contacted"), ("b.io", "Contacted"), ("c.io", "Replied")]:
            await storage.upsert_lead(d, status=st, updated_by="s")
            backdate(storage, d, 200)

        eligible = await storage.prunable_leads(90, ["Not contacted", "Contacted"])
        assert {c.domain for c in eligible} == {"a.io", "b.io"}

    async def test_prune_removes_leads_and_their_history(self, storage):
        await storage.upsert_lead("old.io", status="Not contacted", updated_by="s")
        backdate(storage, "old.io", 200)

        removed = await storage.prune_leads(["old.io"])
        assert removed == 1
        assert await storage.leads_for(["old.io"]) == {}
        assert await storage.lead_history("old.io") == []

    async def test_prune_leaves_everything_else_alone(self, storage):
        await storage.upsert_lead("keep.io", status="Replied", updated_by="s")
        await storage.upsert_lead("go.io", status="Not contacted", updated_by="s")
        backdate(storage, "go.io", 200)

        await storage.prune_leads(["go.io"])
        assert set(await storage.leads_for(["keep.io", "go.io"])) == {"keep.io"}

    async def test_pruning_nothing_is_a_no_op(self, storage):
        assert await storage.prune_leads([]) == 0

    async def test_prune_handles_a_large_batch(self, storage):
        domains = [f"d{i}.io" for i in range(850)]
        for d in domains:
            await storage.upsert_lead(d, status="Not contacted", updated_by="s")
        assert await storage.prune_leads(domains) == 850

    async def test_a_pruned_domain_starts_clean_if_it_returns(self, storage):
        await storage.upsert_lead("gone.io", status="Contacted", updated_by="s")
        await storage.prune_leads(["gone.io"])
        await storage.upsert_lead("gone.io", status="Contacted", updated_by="s2")

        events = await storage.lead_history("gone.io")
        assert len(events) == 1 and events[0].from_status == ""


class TestDeleteOne:
    async def test_delete_removes_lead_and_history(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="s")
        assert await storage.delete_lead("acme.io") is True
        assert await storage.leads_for(["acme.io"]) == {}
        assert await storage.lead_history("acme.io") == []

    async def test_delete_unknown_domain_reports_false(self, storage):
        assert await storage.delete_lead("nobody.io") is False
