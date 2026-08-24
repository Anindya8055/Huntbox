"""Lead status tracking: persistence, hydration onto new fetches, validation."""

import pytest

from app.jobs import JobRegistry
from app.models import DEFAULT_LEAD_STATUS, Product
from app.storage import Storage
from tests.test_storage import make_job

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "leads.db")
    await s.init()
    return s


class TestPersistence:
    async def test_unknown_domain_has_no_stored_lead(self, storage):
        assert await storage.leads_for(["nobody.io"]) == {}

    async def test_set_status_then_read_it_back(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="sam")
        lead = (await storage.leads_for(["acme.io"]))["acme.io"]

        assert lead.status == "Contacted"
        assert lead.updated_by == "sam"
        assert lead.updated_at  # stamped

    async def test_status_update_preserves_existing_note(self, storage):
        await storage.upsert_lead("acme.io", note="left voicemail", updated_by="sam")
        await storage.upsert_lead("acme.io", status="Replied", updated_by="kim")

        lead = (await storage.leads_for(["acme.io"]))["acme.io"]
        assert lead.status == "Replied"
        assert lead.note == "left voicemail"
        assert lead.updated_by == "kim"

    async def test_note_update_preserves_existing_status(self, storage):
        await storage.upsert_lead("acme.io", status="Contacted", updated_by="sam")
        await storage.upsert_lead("acme.io", note="follow up Friday", updated_by="kim")

        lead = (await storage.leads_for(["acme.io"]))["acme.io"]
        assert lead.status == "Contacted"
        assert lead.note == "follow up Friday"

    async def test_note_can_be_cleared_explicitly(self, storage):
        await storage.upsert_lead("acme.io", note="old", updated_by="sam")
        await storage.upsert_lead("acme.io", note="", updated_by="sam")
        assert (await storage.leads_for(["acme.io"]))["acme.io"].note == ""

    async def test_first_write_defaults_to_not_contacted(self, storage):
        await storage.upsert_lead("acme.io", note="just a note", updated_by="sam")
        assert (await storage.leads_for(["acme.io"]))["acme.io"].status == (
            DEFAULT_LEAD_STATUS
        )

    async def test_batch_read_only_returns_known_domains(self, storage):
        await storage.upsert_lead("a.io", status="Contacted", updated_by="s")
        await storage.upsert_lead("b.io", status="Not a fit", updated_by="s")

        leads = await storage.leads_for(["a.io", "b.io", "c.io"])
        assert set(leads) == {"a.io", "b.io"}

    async def test_empty_domain_list_is_a_no_op(self, storage):
        assert await storage.leads_for([]) == {}

    async def test_handles_more_domains_than_the_sqlite_variable_limit(self, storage):
        """Batches are chunked; 900 domains must not blow up the IN clause."""
        await storage.upsert_lead("target.io", status="Replied", updated_by="s")
        domains = [f"d{i}.io" for i in range(900)] + ["target.io"]

        leads = await storage.leads_for(domains)
        assert set(leads) == {"target.io"}

    async def test_all_leads_lists_everything(self, storage):
        await storage.upsert_lead("a.io", status="Contacted", updated_by="s")
        await storage.upsert_lead("b.io", status="Replied", updated_by="s")
        assert {lead.domain for lead in await storage.all_leads()} == {"a.io", "b.io"}


class TestSurvivesRestart:
    async def test_lead_outlives_the_run_that_created_it(self, tmp_path):
        """A trimmed run must not take its leads with it."""
        db = tmp_path / "t.db"
        s1 = Storage(db)
        await s1.init()
        await s1.upsert_lead("acme.io", status="Contacted", updated_by="sam")

        s2 = Storage(db)  # stands in for a restarted process
        await s2.init()
        assert (await s2.leads_for(["acme.io"]))["acme.io"].status == "Contacted"


class TestHydration:
    """The core promise: a known domain carries its status into a new fetch."""

    def products(self, *domains) -> list[Product]:
        return [
            Product(rank=i, product_name=f"P{i}", domain=d)
            for i, d in enumerate(domains, start=1)
        ]

    async def test_known_domain_keeps_its_status_in_a_new_run(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        await reg.storage.upsert_lead(
            "acme.io", status="Contacted", note="emailed Tue", updated_by="sam"
        )

        products = self.products("acme.io", "fresh.io")
        await reg._hydrate_leads(products)

        assert products[0].lead_status == "Contacted"
        assert products[0].lead_note == "emailed Tue"
        assert products[0].lead_updated_by == "sam"
        # An unseen domain stays at the default rather than inheriting anything.
        assert products[1].lead_status == DEFAULT_LEAD_STATUS
        assert products[1].lead_note == ""

    async def test_products_without_a_domain_are_skipped(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        products = self.products("", "")
        await reg._hydrate_leads(products)
        assert all(p.lead_status == DEFAULT_LEAD_STATUS for p in products)

    async def test_hydration_without_storage_is_harmless(self):
        reg = JobRegistry()
        products = self.products("acme.io")
        await reg._hydrate_leads(products)  # must not raise
        assert products[0].lead_status == DEFAULT_LEAD_STATUS

    async def test_stored_status_survives_a_run_being_persisted(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        await reg.storage.upsert_lead("p1.io", status="Not a fit", updated_by="kim")

        job = make_job(n=2, with_emails=2)
        job.results[0].domain = "p1.io"
        await reg._hydrate_leads(job.results)
        await reg._finish(job, "daily")

        restored = await reg.storage.load_last_run()
        assert restored.results[0].lead_status == "Not a fit"
