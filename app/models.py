"""Shared pydantic models for requests, results and job state."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Timeframe = Literal["daily", "weekly", "monthly", "yearly", "custom"]

# Outreach pipeline states, keyed by company domain and shared by the team.
LeadStatus = Literal["Not contacted", "Contacted", "Replied", "Not a fit"]
LEAD_STATUSES: tuple[str, ...] = (
    "Not contacted", "Contacted", "Replied", "Not a fit",
)
DEFAULT_LEAD_STATUS: str = "Not contacted"


class Lead(BaseModel):
    """Stored outreach state for one company domain."""

    domain: str
    status: LeadStatus = DEFAULT_LEAD_STATUS
    note: str = ""
    updated_by: str = ""
    updated_at: str = ""


class LeadEvent(BaseModel):
    """One entry in a lead's append-only history."""

    domain: str
    from_status: str = ""
    to_status: str = ""
    note: str = ""
    note_changed: bool = False
    actor: str = ""
    created_at: str = ""


class PruneRequest(BaseModel):
    """Criteria for clearing out dead lead rows."""

    older_than_days: int = Field(default=90, ge=1, le=3650)
    statuses: list[LeadStatus] = Field(default_factory=lambda: ["Not contacted"])
    # Default is a preview: nothing is deleted unless this is explicitly set.
    confirm: bool = False


class LeadUpdate(BaseModel):
    """PATCH body for /api/leads/{domain}."""

    status: LeadStatus | None = None
    note: str | None = Field(default=None, max_length=2000)
    updated_by: str = Field(default="", max_length=80)


class ScrapeRequest(BaseModel):
    timeframe: Timeframe = "daily"
    limit: int = Field(default=10, ge=1, le=100)
    date_from: date | None = None
    date_to: date | None = None
    enrich: bool = True

    @model_validator(mode="after")
    def _check_custom(self) -> "ScrapeRequest":
        if self.timeframe == "custom":
            if not self.date_from or not self.date_to:
                raise ValueError("custom timeframe requires date_from and date_to")
            if self.date_from > self.date_to:
                raise ValueError("date_from must be on or before date_to")
        return self


class Product(BaseModel):
    """Stage 1 output: a single Product Hunt post."""

    rank: int
    product_name: str
    tagline: str = ""
    description: str = ""
    votes: int = 0
    comments: int = 0
    producthunt_url: str = ""
    website_url: str = ""
    topics: list[str] = Field(default_factory=list)
    launch_date: str = ""

    # Stage 2 fields, filled in by the enrichment provider.
    company_name: str = ""
    company_description: str = ""
    domain: str = ""
    # Ahrefs Domain Rating, 0-100. None when unknown or not looked up.
    domain_rating: float | None = None
    # Domain age in years, from the RDAP registration event. None when
    # unknown, not looked up, or the registry redacts registration dates.
    domain_age_years: float | None = None
    email: str = ""
    # True only when the email's domain matches the resolved company domain.
    email_verified: bool = False
    enrichment_status: Literal[
        "pending", "running", "found", "unverified", "no_email", "error"
    ] = "pending"
    enrichment_note: str = ""

    # Team-shared outreach state, hydrated from the leads table by domain.
    lead_status: LeadStatus = DEFAULT_LEAD_STATUS
    lead_note: str = ""
    lead_updated_by: str = ""
    lead_updated_at: str = ""


class Enrichment(BaseModel):
    """What an enrichment provider returns for one product."""

    company_name: str = ""
    company_description: str = ""
    domain: str = ""
    email: str = ""
    email_verified: bool = False
    note: str = ""


class JobStatus(BaseModel):
    job_id: str
    state: Literal["queued", "fetching", "enriching", "done", "error"] = "queued"
    message: str = ""
    # Non-fatal advisory shown as a banner (e.g. upvotes still hidden).
    notice: str = ""
    total: int = 0
    enriched: int = 0
    results: list[Product] = Field(default_factory=list)
    range_from: str = ""
    range_to: str = ""
    # When Product Hunt actually answered — not when the UI rendered.
    fetched_at: str = ""
