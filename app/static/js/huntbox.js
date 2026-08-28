/* Huntbox — internal lead dashboard controller.
   Fetch -> poll -> render a dense sortable table. Status edits persist
   immediately via PATCH /api/leads/{domain}. */

(() => {
  "use strict";

  const POLL_MS = 700;
  const DR_MID_MAX = 50;    // above the "good" threshold but still worth a look

  // Filter defaults. Nothing narrows the set by default -- a hunt for N
  // should show N rows -- so drMax stays wide open; "Good <= 20" is only a
  // colour-coding threshold, not a visibility cutoff. Filters are all still
  // available to narrow manually.
  const DEFAULTS = {
    search: "", drMin: 0, drMax: 100, drUnknown: true, minVotes: 0,
    emailOnly: false, hasDomain: false, topics: [], hideWorked: true,
    sort: "opportunity:desc", threshold: 20,
  };
  const WORKED_STATUSES = ["Contacted", "Not a fit"];
  const PREFS_KEY = "huntbox.filters";

  const $ = (s) => document.querySelector(s);
  const el = {
    form: $("#hunt-form"),
    btn: $("#hunt-btn"),
    btnLabel: $("#hunt-label"),
    limit: $("#limit-select"),
    rangeHint: $("#range-hint"),
    dates: $("#custom-dates"),
    dateFrom: $("#date-from"),
    dateTo: $("#date-to"),
    progress: $("#progress"),
    fill: $("#progress-fill"),
    msg: $("#progress-msg"),
    banners: $("#banner-slot"),
    section: $("#results-section"),
    stats: $("#results-stats"),
    tbody: $("#grid-body"),
    thead: $("#grid").tHead,
    foot: $("#table-foot"),
    empty: $("#empty-state"),
    emptyTitle: $("#empty-title"),
    emptyBody: $("#empty-body"),
    idle: $("#idle-state"),
    fetched: $("#fetched-at"),
    refresh: $("#refresh-btn"),
    settingsApify: $("#settings-apify"),
    settingsSerper: $("#settings-serper"),
    settingsSave: $("#settings-save"),
    filters: $("#filters"),
    fSearch: $("#f-search"),
    fDrMin: $("#f-dr-min"),
    fDrMax: $("#f-dr-max"),
    fDrUnknown: $("#f-dr-unknown"),
    fVotes: $("#f-votes"),
    fEmail: $("#f-email"),
    fDomain: $("#f-domain"),
    fTopics: $("#f-topics"),
    fWorked: $("#f-worked"),
    fSort: $("#f-sort"),
    fThreshold: $("#f-threshold"),
    fReset: $("#f-reset"),
    fCount: $("#f-count"),
    copyAll: $("#copy-all"),
    histPop: $("#history-pop"),
    histDomain: $("#history-domain"),
    histList: $("#history-list"),
    histClose: $("#history-close"),
    pruneBtn: $("#prune-btn"),
    prunePop: $("#prune-pop"),
    pruneClose: $("#prune-close"),
    pruneDays: $("#prune-days"),
    pruneResult: $("#prune-result"),
    prunePreview: $("#prune-preview"),
    pruneConfirm: $("#prune-confirm"),
  };

  const state = {
    timeframe: "daily",
    limit: 25,
    polling: null,
    rows: [],
    sortKey: "opportunity",
    sortDir: "desc",      // best targets first
    noticeShown: false,
    fetchedAt: null,
    f: loadPrefs(),
  };

  function loadPrefs() {
    try {
      return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(PREFS_KEY)) || {}) };
    } catch {
      return { ...DEFAULTS };
    }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.f)); } catch { /* quota */ }
  }

  const STATUS_CLASS = {
    "Not contacted": "status--new",
    "Contacted": "status--con",
    "Replied": "status--rep",
    "Not a fit": "status--nofit",
  };
  const STATUSES = Object.keys(STATUS_CLASS);

  /* ── API key settings ────────────────────────────────── */

  // Write-only: these fields never get pre-filled with the current secret,
  // only whichever value is typed gets sent, and the server never echoes
  // the token back. Leaving a field blank means "keep the current key."
  async function saveSettings() {
    const body = {};
    const apify = el.settingsApify.value.trim();
    const serper = el.settingsSerper.value.trim();
    if (apify) body.apify_api_token = apify;
    if (serper) body.serper_api_key = serper;
    if (!Object.keys(body).length) {
      banner("warn", "Nothing to save", "Enter a token first.");
      return;
    }
    try {
      const res = await fetch("/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(detailOf(data, res.status));
      el.settingsApify.value = "";
      el.settingsSerper.value = "";
      flash(el.settingsSave, "Saved");
      refreshKeyDots();
    } catch (e) {
      banner("error", "Save failed", String(e.message || e));
    }
  }
  el.settingsSave.addEventListener("click", saveSettings);

  async function refreshKeyDots() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      const map = { PH: data.producthunt_token, SE: data.serper_key, AH: data.ahrefs_key, AP: data.apify_key };
      document.querySelectorAll(".keys i").forEach((i) => {
        const key = i.textContent.trim();
        if (key in map) i.className = map[key] ? "on" : "off";
      });
    } catch { /* best effort, dots just stay as they were */ }
  }

  /* ── Range preview (mirrors app/timeframes.py) ────────── */

  const fmt = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });

  function previewRange() {
    const n = new Date();
    const utc = new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate()));
    let start;
    switch (state.timeframe) {
      case "daily": start = utc; break;
      case "weekly": {
        const dow = (utc.getUTCDay() + 6) % 7;
        start = new Date(utc); start.setUTCDate(utc.getUTCDate() - dow);
        break;
      }
      case "monthly": start = new Date(Date.UTC(utc.getUTCFullYear(), utc.getUTCMonth(), 1)); break;
      case "yearly":  start = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1)); break;
      case "custom":
        return el.dateFrom.value && el.dateTo.value
          ? `${el.dateFrom.value} → ${el.dateTo.value}` : "pick dates";
    }
    return start.getTime() === utc.getTime()
      ? `${fmt(utc)} (today)` : `${fmt(start)} → ${fmt(utc)}`;
  }
  function refreshHint() { el.rangeHint.textContent = previewRange(); }

  document.querySelectorAll("[data-timeframe]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-timeframe]").forEach((b) => {
        b.classList.toggle("is-active", b === btn);
        b.setAttribute("aria-checked", b === btn ? "true" : "false");
      });
      state.timeframe = btn.dataset.timeframe;
      el.dates.hidden = state.timeframe !== "custom";
      refreshHint();
    });
  });
  el.limit.addEventListener("change", () => { state.limit = parseInt(el.limit.value, 10); });
  [el.dateFrom, el.dateTo].forEach((i) => i.addEventListener("change", refreshHint));
  refreshHint();

  /* ── Banners ──────────────────────────────────────────── */

  const clearBanners = () => { el.banners.innerHTML = ""; };

  function banner(kind, title, body) {
    const d = document.createElement("div");
    d.className = `banner banner--${kind}`;
    d.innerHTML = "<div><b></b><p></p></div>";
    d.querySelector("b").textContent = title;
    d.querySelector("p").textContent = body;
    el.banners.appendChild(d);
  }

  /* ── Cell builders ────────────────────────────────────── */

  /* Opportunity: traction × weakness. High votes against a low-DR domain is
     exactly the pitch, so this collapses the two columns people were
     eyeballing together into one sortable number. */
  function computeOpportunity(rows) {
    const maxVotes = Math.max(1, ...rows.map((r) => r.votes || 0));
    rows.forEach((r) => {
      if (!hasDr(r)) { r.opportunity = null; return; }
      const traction = Math.sqrt((r.votes || 0) / maxVotes);   // flatten the long tail
      const weakness = 1 - Math.min(100, r.domain_rating) / 100;
      // An older weak-DR site is an even better outreach target -- an
      // established company that still hasn't invested in SEO, versus a
      // young one that just hasn't had time yet. Subtle nudge, not a driver.
      const age = r.domain_age_years;
      const maturity = age === null || age === undefined ? 0.85 : Math.min(1, 0.5 + age / 20);
      r.opportunity = Math.round(traction * weakness * maturity * 100);
    });
  }

  function oppCell(row) {
    const td = document.createElement("td");
    td.className = "score";
    const v = row.opportunity;
    const band = v === null ? "none" : v >= 55 ? "hi" : v >= 25 ? "mid" : "lo";

    const wrap = document.createElement("div");
    wrap.className = `opp opp--${band}`;
    wrap.innerHTML =
      `<span class="opp__bar"><i style="width:${v === null ? 0 : v}%"></i></span>` +
      `<span class="opp__val">${v === null ? "–" : v}</span>`;
    wrap.title = v === null
      ? "No DR resolved — can't score this one"
      : `Opportunity ${v}/100 · ${row.votes} votes against DR ${Math.round(row.domain_rating)}`;
    td.appendChild(wrap);
    return td;
  }

  function drCell(dr) {
    const td = document.createElement("td");
    td.className = "num";
    if (dr === null || dr === undefined) {
      td.innerHTML = '<span class="dr dr--none">—</span>';
      return td;
    }
    const v = Math.round(dr);
    const good = state.f.threshold;
    const band = v <= good ? "good" : v <= Math.max(good, DR_MID_MAX) ? "mid" : "weak";
    const colour = band === "good" ? "var(--good)" : band === "mid" ? "var(--mid)" : "var(--muted)";

    const wrap = document.createElement("div");
    wrap.className = "drwrap";
    wrap.innerHTML =
      `<span class="dr dr--${band}">${v}</span>` +
      `<span class="drgauge"><i style="width:${Math.max(2, v)}%;background:${colour}"></i></span>`;
    wrap.title =
      band === "good" ? `DR ${v} — low authority, strong SEO lead`
      : band === "mid" ? `DR ${v} — mid authority`
      : `DR ${v} — established, weaker lead`;
    td.appendChild(wrap);
    return td;
  }

  function ageCell(row) {
    const td = document.createElement("td");
    td.className = "num";
    const age = row.domain_age_years;
    if (age === null || age === undefined) {
      td.innerHTML = '<span class="dr dr--none">—</span>';
      return td;
    }
    const span = document.createElement("span");
    span.className = "dr";
    span.style.color = "var(--accent-2)";
    span.textContent = `${age}y`;
    span.title = `Registered roughly ${age} year${age === 1 ? "" : "s"} ago (RDAP)`;
    td.appendChild(span);
    return td;
  }

  function statusCell(row) {
    const td = document.createElement("td");
    if (!row.domain) {
      td.innerHTML = '<span class="dnone" title="No domain resolved — nothing stable to track">—</span>';
      return td;
    }
    const sel = document.createElement("select");
    sel.className = `status ${STATUS_CLASS[row.lead_status] || "status--new"}`;
    STATUSES.forEach((s) => {
      const o = document.createElement("option");
      o.value = s; o.textContent = s;
      o.selected = s === row.lead_status;
      sel.appendChild(o);
    });
    if (row.lead_updated_by) {
      sel.title = `${row.lead_status} — ${row.lead_updated_by}, ${row.lead_updated_at || ""}`;
    }
    sel.addEventListener("change", () => saveStatus(row, sel));
    td.appendChild(sel);
    return td;
  }

  async function saveStatus(row, sel) {
    const next = sel.value;
    sel.disabled = true;
    try {
      const res = await fetch(`/api/leads/${encodeURIComponent(row.domain)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const lead = await res.json();
      row.lead_status = lead.status;
      row.lead_updated_by = lead.updated_by;
      row.lead_updated_at = lead.updated_at;
      sel.className = `status ${STATUS_CLASS[lead.status]}`;
      sel.title = `${lead.status} — ${lead.updated_by}, ${lead.updated_at}`;
      // Deliberately not repainting: a row disappearing under the cursor
      // makes a mis-click hard to undo. Dim it instead; it drops out on the
      // next filter change or fetch.
      sel.closest("tr")?.classList.toggle(
        "is-worked", state.f.hideWorked && WORKED_STATUSES.includes(lead.status)
      );
      refreshCounts();
    } catch {
      sel.value = row.lead_status;   // roll back the optimistic change
      banner("error", "Couldn't save status", `${row.domain} was not updated. Check the server.`);
    } finally {
      sel.disabled = false;
    }
  }

  function buildRow(row) {
    const tr = document.createElement("tr");
    if (row.enrichment_status === "pending" || row.enrichment_status === "running") {
      tr.className = "is-pending";
    }

    const rank = document.createElement("td");
    rank.className = "rank";
    rank.textContent = row.rank;
    tr.appendChild(rank);

    const prod = document.createElement("td");
    prod.className = "product";
    const nm = document.createElement("div");
    nm.className = "pname"; nm.textContent = row.product_name;
    const tg = document.createElement("div");
    tg.className = "ptag"; tg.textContent = row.tagline || "";
    prod.append(nm, tg);
    tr.appendChild(prod);

    tr.appendChild(oppCell(row));
    if (row.opportunity !== null && row.opportunity >= 55) tr.classList.add("is-target");

    const votes = document.createElement("td");
    votes.className = "num votes"; votes.textContent = row.votes ?? 0;
    tr.appendChild(votes);

    const comments = document.createElement("td");
    comments.className = "num comments"; comments.textContent = row.comments ?? 0;
    tr.appendChild(comments);

    tr.appendChild(drCell(row.domain_rating));
    tr.appendChild(ageCell(row));

    const dom = document.createElement("td");
    if (row.domain) {
      const a = document.createElement("a");
      a.className = "dlink"; a.href = `https://${row.domain}`;
      a.target = "_blank"; a.rel = "noopener noreferrer";
      a.textContent = row.domain;
      dom.appendChild(a);
    } else if (row.enrichment_status === "pending" || row.enrichment_status === "running") {
      dom.innerHTML = '<span class="sk"></span>';
    } else {
      dom.innerHTML = '<span class="dnone">not resolved</span>';
    }
    tr.appendChild(dom);

    const mail = document.createElement("td");
    mail.className = "email";
    if (row.email) {
      const a = document.createElement("a");
      a.className = `mail${row.email_verified ? "" : " mail--unv"}`;
      a.href = `mailto:${row.email}`;
      a.textContent = row.email;
      if (!row.email_verified) {
        const f = document.createElement("span");
        f.className = "vflag"; f.textContent = "unverified";
        f.title = "No confirmed company domain — check before using";
        a.appendChild(f);
      }
      mail.appendChild(a);
    } else if (row.enrichment_status === "pending" || row.enrichment_status === "running") {
      mail.innerHTML = '<span class="sk"></span>';
    } else {
      mail.innerHTML = '<span class="mail mail--none">—</span>';
    }
    tr.appendChild(mail);

    const topics = document.createElement("td");
    topics.className = "topics";
    const wrap = document.createElement("div");
    wrap.className = "tags";
    (row.topics || []).slice(0, 3).forEach((t) => {
      const s = document.createElement("span");
      s.className = "tag"; s.textContent = t;
      wrap.appendChild(s);
    });
    topics.appendChild(wrap);
    tr.appendChild(topics);

    tr.appendChild(statusCell(row));

    const acts = document.createElement("td");
    const box = document.createElement("div");
    box.className = "rowacts";
    if (row.domain) {
      const hist = document.createElement("button");
      hist.type = "button";
      hist.className = "iconbtn iconbtn--hist";
      hist.textContent = "◷";
      hist.title = row.lead_updated_by
        ? `History — last: ${row.lead_status} by ${row.lead_updated_by}`
        : "Change history";
      hist.addEventListener("click", (e) => {
        e.stopPropagation();
        showHistory(row.domain, hist);
      });
      box.appendChild(hist);
    }

    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "iconbtn iconbtn--copy";
    copy.textContent = "⧉";
    copy.title = `Copy: ${rowSummary(row)}`;
    copy.addEventListener("click", async () => {
      const ok = await copyText(rowSummary(row));
      flash(copy, ok ? "✓" : "✕", ok);
    });
    box.appendChild(copy);

    const ph = document.createElement("a");
    ph.className = "iconbtn"; ph.href = row.producthunt_url;
    ph.target = "_blank"; ph.rel = "noopener noreferrer";
    ph.textContent = "PH"; ph.title = "Open on Product Hunt";
    box.appendChild(ph);
    acts.appendChild(box);
    tr.appendChild(acts);

    return tr;
  }

  /* ── Popover plumbing ─────────────────────────────────── */

  let scrim = null;

  function openPop(node, anchor) {
    closePops();
    node.hidden = false;
    if (anchor) {
      const r = anchor.getBoundingClientRect();
      const w = node.offsetWidth || 340;
      node.style.top = `${window.scrollY + r.bottom + 6}px`;
      node.style.left = `${Math.max(8, Math.min(
        window.scrollX + r.right - w, window.scrollX + window.innerWidth - w - 8
      ))}px`;
    } else {
      scrim = document.createElement("div");
      scrim.className = "scrim";
      scrim.addEventListener("click", closePops);
      document.body.appendChild(scrim);
    }
  }

  function closePops() {
    el.histPop.hidden = true;
    el.prunePop.hidden = true;
    if (scrim) { scrim.remove(); scrim = null; }
  }

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePops(); });
  el.histClose.addEventListener("click", closePops);
  el.pruneClose.addEventListener("click", closePops);
  document.addEventListener("click", (e) => {
    if (el.histPop.hidden) return;
    if (!el.histPop.contains(e.target) && !e.target.closest(".iconbtn--hist")) closePops();
  });

  /* ── Lead history ─────────────────────────────────────── */

  const relTime = (iso) => {
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return iso || "";
    const s = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return t.toLocaleDateString();
  };

  async function showHistory(domain, anchor) {
    el.histDomain.textContent = domain;
    el.histList.innerHTML = '<li class="ev__empty">Loading…</li>';
    openPop(el.histPop, anchor);

    let events = [];
    try {
      const res = await fetch(`/api/leads/${encodeURIComponent(domain)}/history`);
      events = (await res.json()).events || [];
    } catch {
      el.histList.innerHTML = '<li class="ev__empty">Could not load history.</li>';
      return;
    }

    if (!events.length) {
      el.histList.innerHTML = '<li class="ev__empty">No changes recorded yet.</li>';
      return;
    }

    el.histList.replaceChildren(...events.map((ev) => {
      const li = document.createElement("li");
      const line = document.createElement("div");
      line.className = "ev__line";
      if (ev.from_status && ev.from_status !== ev.to_status) {
        const f = document.createElement("span");
        f.className = "ev__from"; f.textContent = ev.from_status;
        const a = document.createElement("span");
        a.className = "ev__arrow"; a.textContent = "→";
        line.append(f, a);
      }
      const to = document.createElement("span");
      to.className = "ev__to"; to.textContent = ev.to_status;
      line.appendChild(to);
      if (ev.note_changed) {
        const n = document.createElement("span");
        n.className = "ev__arrow"; n.textContent = "· note edited";
        line.appendChild(n);
      }
      li.appendChild(line);

      const meta = document.createElement("div");
      meta.className = "ev__meta";
      meta.textContent = `${ev.actor || "anon"} · ${relTime(ev.created_at)}`;
      meta.title = ev.created_at;
      li.appendChild(meta);

      if (ev.note_changed && ev.note) {
        const note = document.createElement("div");
        note.className = "ev__note"; note.textContent = `"${ev.note}"`;
        li.appendChild(note);
      }
      return li;
    }));
  }

  /* ── Pruning ──────────────────────────────────────────── */

  const pruneStatuses = () =>
    [...el.prunePop.querySelectorAll(".pop__set input:checked")].map((i) => i.value);

  function prunePayload(confirm) {
    return {
      older_than_days: clampNum(el.pruneDays.value, 1, 3650, 90),
      statuses: pruneStatuses(),
      confirm,
    };
  }

  async function pruneRequest(confirm) {
    const statuses = pruneStatuses();
    if (!statuses.length) {
      el.pruneResult.textContent = "Pick at least one status.";
      el.pruneResult.className = "pop__result warn";
      return null;
    }
    const res = await fetch("/api/leads/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prunePayload(confirm)),
    });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  el.pruneBtn.addEventListener("click", () => {
    el.pruneResult.textContent = "";
    el.pruneResult.className = "pop__result";
    el.pruneConfirm.disabled = true;
    openPop(el.prunePop, null);
  });

  el.prunePreview.addEventListener("click", async () => {
    try {
      const data = await pruneRequest(false);
      if (!data) return;
      const n = data.eligible;
      el.pruneResult.className = `pop__result${n ? " warn" : ""}`;
      el.pruneResult.textContent = n
        ? `${n} lead${n === 1 ? "" : "s"} would be deleted, history included.`
        : "Nothing matches — no leads to prune.";
      el.pruneConfirm.disabled = n === 0;
      el.pruneConfirm.textContent = n ? `Delete ${n}` : "Delete";
    } catch {
      el.pruneResult.textContent = "Preview failed — check the server.";
      el.pruneResult.className = "pop__result warn";
    }
  });

  el.pruneConfirm.addEventListener("click", async () => {
    el.pruneConfirm.disabled = true;
    try {
      const data = await pruneRequest(true);
      if (!data) return;
      el.pruneResult.className = "pop__result";
      el.pruneResult.textContent = `Deleted ${data.removed} lead${data.removed === 1 ? "" : "s"}.`;
      el.pruneConfirm.textContent = "Delete";
      // Any pruned domain in view reverts to its default state.
      const gone = new Set((data.sample || []).map((s) => s.domain));
      state.rows.forEach((r) => {
        if (gone.has(r.domain)) {
          r.lead_status = "Not contacted";
          r.lead_note = ""; r.lead_updated_by = ""; r.lead_updated_at = "";
        }
      });
      paint();
    } catch {
      el.pruneResult.textContent = "Delete failed — check the server.";
      el.pruneResult.className = "pop__result warn";
    }
  });

  /* ── Clipboard ────────────────────────────────────────── */

  /** navigator.clipboard needs a secure context and can be blocked; fall
   *  back to a hidden textarea so copy still works over plain http. */
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* fall through */ }

    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0;";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }

  /** Flash a button's label, then restore it. */
  function flash(btn, message, ok = true) {
    if (btn.dataset.flashing) return;
    const original = btn.textContent;
    btn.dataset.flashing = "1";
    btn.textContent = message;
    btn.classList.add(ok ? "ok" : "bad");
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove("ok", "bad");
      delete btn.dataset.flashing;
    }, 1100);
  }

  /** One-line summary: Product | domain | DR n | email */
  function rowSummary(r) {
    return [
      r.product_name,
      r.domain || "no domain",
      hasDr(r) ? `DR ${Math.round(r.domain_rating)}` : "DR ?",
      r.email || "no email",
    ].join(" | ");
  }

  const TSV_COLUMNS = [
    ["Rank", (r) => r.rank],
    ["Product", (r) => r.product_name],
    ["Tagline", (r) => r.tagline],
    ["Votes", (r) => r.votes ?? 0],
    ["Comments", (r) => r.comments ?? 0],
    ["DR", (r) => (hasDr(r) ? Math.round(r.domain_rating) : "")],
    ["Age (yrs)", (r) => (r.domain_age_years ?? "")],
    ["Domain", (r) => r.domain],
    ["Email", (r) => r.email],
    ["Verified", (r) => (r.email ? (r.email_verified ? "yes" : "no") : "")],
    ["Status", (r) => r.lead_status],
    ["Topics", (r) => (r.topics || []).join(", ")],
    ["Launched", (r) => r.launch_date],
    ["Product Hunt", (r) => r.producthunt_url],
  ];

  /** Tabs and newlines would break column alignment on paste. */
  const tsvSafe = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ").trim();

  function rowsToTsv(rows) {
    const lines = [TSV_COLUMNS.map(([h]) => h).join("\t")];
    rows.forEach((r) => {
      lines.push(TSV_COLUMNS.map(([, get]) => tsvSafe(get(r))).join("\t"));
    });
    return lines.join("\n");
  }

  el.copyAll.addEventListener("click", async () => {
    const rows = sorted(visibleRows());
    if (!rows.length) { flash(el.copyAll, "Nothing to copy", false); return; }
    const ok = await copyText(rowsToTsv(rows));
    flash(el.copyAll, ok ? `Copied ${rows.length}!` : "Copy failed", ok);
  });

  /* ── Filtering (client-side, no refetch) ──────────────── */

  function visibleRows() {
    const f = state.f;
    const q = f.search.trim().toLowerCase();
    const topics = new Set(f.topics);

    return state.rows.filter((r) => {
      if (q) {
        const hay = `${r.product_name} ${r.tagline}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if ((r.votes ?? 0) < f.minVotes) return false;
      if (f.emailOnly && !r.email) return false;
      if (f.hasDomain && !r.domain) return false;

      const dr = r.domain_rating;
      if (dr === null || dr === undefined) {
        if (!f.drUnknown) return false;
      } else if (dr < f.drMin || dr > f.drMax) {
        return false;
      }

      if (topics.size && !(r.topics || []).some((t) => topics.has(t))) return false;

      // Only hide worked leads that someone actually worked; "Replied" stays
      // visible because it's an open conversation.
      if (f.hideWorked && WORKED_STATUSES.includes(r.lead_status)) return false;

      return true;
    });
  }

  function syncFilterUI() {
    const f = state.f;
    el.fSearch.value = f.search;
    el.fDrMin.value = f.drMin;
    el.fDrMax.value = f.drMax;
    el.fDrUnknown.checked = f.drUnknown;
    el.fVotes.value = f.minVotes;
    el.fEmail.checked = f.emailOnly;
    el.fDomain.checked = f.hasDomain;
    el.fWorked.checked = f.hideWorked;
    el.fSort.value = f.sort;
    el.fThreshold.value = f.threshold;

    // Highlight controls that are actually narrowing the set.
    const on = (node, active) => node.closest(".f")?.classList.toggle("is-on", active);
    on(el.fSearch, !!f.search);
    on(el.fDrMin, f.drMin !== DEFAULTS.drMin || f.drMax !== DEFAULTS.drMax);
    on(el.fVotes, f.minVotes > 0);
    on(el.fEmail, f.emailOnly);
    on(el.fDomain, f.hasDomain);
    on(el.fTopics, f.topics.length > 0);
    on(el.fWorked, f.hideWorked);
    el.fDrUnknown.closest(".f")?.classList.toggle("is-on", !f.drUnknown);
  }

  function rebuildTopicOptions() {
    const counts = new Map();
    state.rows.forEach((r) => (r.topics || []).forEach((t) => {
      counts.set(t, (counts.get(t) || 0) + 1);
    }));
    const chosen = new Set(state.f.topics);
    const opts = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

    el.fTopics.replaceChildren(...opts.map(([name, n]) => {
      const o = document.createElement("option");
      o.value = name;
      o.textContent = `${name} (${n})`;
      o.selected = chosen.has(name);
      return o;
    }));
    // Drop selections that no longer exist in this result set.
    state.f.topics = state.f.topics.filter((t) => counts.has(t));
  }

  function onFilterChange() {
    const f = state.f;
    f.search = el.fSearch.value;
    f.drMin = clampNum(el.fDrMin.value, 0, 100, DEFAULTS.drMin);
    f.drMax = clampNum(el.fDrMax.value, 0, 100, DEFAULTS.drMax);
    if (f.drMin > f.drMax) [f.drMin, f.drMax] = [f.drMax, f.drMin];
    f.drUnknown = el.fDrUnknown.checked;
    f.minVotes = clampNum(el.fVotes.value, 0, 1e6, 0);
    f.emailOnly = el.fEmail.checked;
    f.hasDomain = el.fDomain.checked;
    f.topics = [...el.fTopics.selectedOptions].map((o) => o.value);
    f.hideWorked = el.fWorked.checked;
    f.threshold = clampNum(el.fThreshold.value, 1, 100, DEFAULTS.threshold);

    const [key, dir] = el.fSort.value.split(":");
    f.sort = el.fSort.value;
    state.sortKey = key;
    state.sortDir = dir;

    savePrefs();
    paint();
  }

  function clampNum(raw, lo, hi, fallback) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return fallback;
    return Math.min(hi, Math.max(lo, n));
  }

  let searchTimer = null;
  el.fSearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(onFilterChange, 120);   // live, but not per-keystroke reflow
  });
  [el.fDrMin, el.fDrMax, el.fVotes, el.fThreshold].forEach((n) =>
    n.addEventListener("input", onFilterChange)
  );
  [el.fDrUnknown, el.fEmail, el.fDomain, el.fWorked, el.fTopics, el.fSort].forEach((n) =>
    n.addEventListener("change", onFilterChange)
  );
  function resetFilters() {
    state.f = { ...DEFAULTS, threshold: state.f.threshold };
    const [k, d] = state.f.sort.split(":");
    state.sortKey = k; state.sortDir = d;
    [...el.fTopics.options].forEach((o) => (o.selected = false));
    savePrefs();
    syncFilterUI();
  }
  el.fReset.addEventListener("click", () => { resetFilters(); paint(); });

  /* ── Sorting ──────────────────────────────────────────── */

  function sorted(rows) {
    const k = state.sortKey;
    const dir = state.sortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      let x = a[k], y = b[k];
      // Unknowns always sink, regardless of direction.
      const xn = x === null || x === undefined || x === "";
      const yn = y === null || y === undefined || y === "";
      if (xn && yn) return a.rank - b.rank;
      if (xn) return 1;
      if (yn) return -1;
      if (typeof x === "string") { x = x.toLowerCase(); y = String(y).toLowerCase(); }
      if (x < y) return -1 * dir;
      if (x > y) return 1 * dir;
      return a.rank - b.rank;
    });
  }

  // Restore persisted sort choice before the first paint.
  (() => {
    const [k, d] = (state.f.sort || DEFAULTS.sort).split(":");
    state.sortKey = k;
    state.sortDir = d;
    syncFilterUI();
  })();

  function wireSort() {
    el.thead.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const k = th.dataset.sort;
        if (state.sortKey === k) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = k;
          // Numeric columns are most useful descending first, except DR.
          state.sortDir = ["votes", "comments", "opportunity"].includes(k) ? "desc" : "asc";
        }
        // Keep the Sort dropdown honest about what the table is doing.
        const combo = `${state.sortKey}:${state.sortDir}`;
        state.f.sort = combo;
        el.fSort.value = [...el.fSort.options].some((o) => o.value === combo) ? combo : "";
        savePrefs();
        paint();
      });
    });
  }
  wireSort();

  function markSortHeader() {
    el.thead.querySelectorAll("th[data-sort]").forEach((th) => {
      const on = th.dataset.sort === state.sortKey;
      th.classList.toggle("is-sorted", on);
      th.classList.toggle("asc", on && state.sortDir === "asc");
      th.classList.toggle("desc", on && state.sortDir === "desc");
    });
  }

  /* ── Paint ────────────────────────────────────────────── */

  function paint() {
    computeOpportunity(state.rows);
    const shown = sorted(visibleRows());
    const frag = document.createDocumentFragment();
    shown.forEach((r) => frag.appendChild(buildRow(r)));
    el.tbody.replaceChildren(frag);
    markSortHeader();
    syncFilterUI();

    const t = state.f.threshold;
    const withEmail = state.rows.filter((r) => r.email).length;
    const lowDr = state.rows.filter((r) => hasDr(r) && r.domain_rating <= t).length;
    const worked = state.rows.filter((r) => WORKED_STATUSES.includes(r.lead_status)).length;

    el.stats.replaceChildren(
      ...[
        ["total", state.rows.length, false],
        ["targets", state.rows.filter((r) => (r.opportunity ?? 0) >= 55).length, true],
        ["with email", withEmail, false],
        [`DR ≤ ${t}`, lowDr, false],
        ["worked", worked, false],
      ].map(([label, value, good]) => {
        const d = document.createElement("div");
        d.className = `kpi${good ? " kpi--good" : ""}`;
        d.innerHTML = `<b>${value}</b><span>${label}</span>`;
        return d;
      })
    );

    const hidden = state.rows.length - shown.length;
    el.fCount.innerHTML =
      `<b>${shown.length}</b> of ${state.rows.length}` +
      (hidden ? ` · ${hidden} filtered out` : "");
    const label = $("#sort-label");
    if (label) {
      const names = {
        opportunity: "opportunity", domain_rating: "DR", votes: "votes",
        comments: "comments", launch_date: "launch date", rank: "rank",
        product_name: "name", domain: "domain", email: "email",
        lead_status: "status", domain_age_years: "age",
      };
      label.textContent = `${names[state.sortKey] || state.sortKey} ${
        state.sortDir === "asc" ? "ascending" : "descending"
      }`;
    }

    el.foot.textContent = shown.length
      ? `${shown.length} rows · sorted by ${state.sortKey} ${state.sortDir}`
      : "No rows match these filters — widen the DR range or hit Reset.";
  }

  const hasDr = (r) => r.domain_rating !== null && r.domain_rating !== undefined;

  /* KPI/count refresh without rebuilding rows (used after a status edit). */
  function refreshCounts() {
    const t = state.f.threshold;
    const worked = state.rows.filter((r) => WORKED_STATUSES.includes(r.lead_status)).length;
    const kpis = el.stats.querySelectorAll(".kpi b");
    if (kpis.length === 5) kpis[4].textContent = worked;

    const shown = visibleRows().length;
    const dimmed = el.tbody.querySelectorAll("tr.is-worked").length;
    const hidden = state.rows.length - shown;
    el.fCount.innerHTML =
      `<b>${shown}</b> of ${state.rows.length}` +
      (hidden ? ` · ${hidden} filtered out` : "") +
      (dimmed ? ` · ${dimmed} worked, drops on refilter` : "");
  }

  /** Uses the server's fetch stamp, not render time, so the label reports
   *  how old the Product Hunt data actually is. */
  function setFetched(iso) {
    state.fetchedAt = iso ? new Date(iso) : null;
    renderFetched();
  }

  function renderFetched() {
    const t = state.fetchedAt;
    if (!t || Number.isNaN(t.getTime())) { el.fetched.textContent = ""; return; }
    const secs = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
    const age = secs < 60 ? `${secs}s`
      : secs < 3600 ? `${Math.floor(secs / 60)}m`
      : `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
    const clock = t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    el.fetched.textContent = `fetched ${clock} · ${age} ago`;
    el.fetched.classList.toggle("stale", secs > 900);   // 15 min
    el.fetched.title = `Product Hunt answered at ${t.toLocaleString()}`;
  }
  setInterval(renderFetched, 15000);

  /* Density: comfortable by default, compact for long scans. */
  const densityBtn = $("#density-btn");
  const applyDensity = () => {
    const on = localStorage.getItem("huntbox.dense") === "1";
    document.body.classList.toggle("dense", on);
    densityBtn.classList.toggle("on", on);
    densityBtn.title = on ? "Comfortable rows" : "Compact rows";
  };
  densityBtn.addEventListener("click", () => {
    localStorage.setItem(
      "huntbox.dense",
      localStorage.getItem("huntbox.dense") === "1" ? "0" : "1"
    );
    applyDensity();
  });
  applyDensity();

  /* ── Run lifecycle ────────────────────────────────────── */

  function setBusy(b) {
    el.btn.disabled = b;
    el.btnLabel.textContent = b ? "Hunting…" : "Hunt";
    el.refresh.disabled = b;
  }

  async function startHunt() {
    clearBanners();
    el.idle.hidden = true;
    el.empty.hidden = true;
    // A leftover narrow filter from a past session (e.g. DR capped at 20)
    // must never silently hide rows from a brand-new hunt -- every fetched
    // result should be visible by default, and filtering stays a manual,
    // per-search action the user opts back into via the filter bar.
    resetFilters();
    setBusy(true);
    el.progress.hidden = false;
    el.fill.style.width = "5%";
    el.msg.textContent = "Fetching from Product Hunt…";
    state.noticeShown = false;

    const body = { timeframe: state.timeframe, limit: state.limit };
    if (state.timeframe === "custom") {
      if (!el.dateFrom.value || !el.dateTo.value) {
        fail("Pick a range", "Custom mode needs both dates.");
        return;
      }
      body.date_from = el.dateFrom.value;
      body.date_to = el.dateTo.value;
    }

    try {
      const res = await fetch("/api/scrape", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { fail("Couldn't start", detailOf(data, res.status)); return; }
      if (data.warning) banner("warn", "Enrichment off", data.warning);
      poll(data.job_id);
    } catch {
      fail("Network error", "Could not reach the Huntbox server.");
    }
  }

  el.form.addEventListener("submit", (e) => { e.preventDefault(); if (!el.btn.disabled) startHunt(); });
  el.refresh.addEventListener("click", () => { if (!el.btn.disabled) startHunt(); });

  function detailOf(data, status) {
    const d = data && data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) {
      return (d[0].msg || "Invalid request.").replace(/^Value error,\s*/i, "");
    }
    return `HTTP ${status}.`;
  }

  function fail(title, body) {
    stopPolling(); setBusy(false); el.progress.hidden = true;
    banner("error", title, body);
  }

  function stopPolling() {
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
  }

  function poll(jobId) {
    stopPolling();
    const tick = async () => {
      let job;
      try {
        const res = await fetch(`/api/scrape/${jobId}/status`);
        if (!res.ok) {
          fail("Lost the run", detailOf(await res.json().catch(() => ({})), res.status));
          return;
        }
        job = await res.json();
      } catch { fail("Network error", "Lost contact mid-run."); return; }

      render(job);

      if (job.state === "done" || job.state === "error") {
        stopPolling(); setBusy(false);
        if (job.notice && !state.noticeShown) {
          state.noticeShown = true;
          banner("warn", "Heads up", job.notice);
        }
        if (job.state === "error") {
          el.progress.hidden = true;
          banner("error", "Hunt failed", job.message);
        } else {
          setFetched(job.fetched_at);
          el.fill.style.width = "100%";
          el.msg.textContent = job.message;
          setTimeout(() => { el.progress.hidden = true; }, 1000);
        }
      }
    };
    tick();
    state.polling = setInterval(tick, POLL_MS);
  }

  function render(job) {
    const results = job.results || [];

    if (job.state === "done" && results.length === 0) {
      el.section.hidden = true;
      el.empty.hidden = false;
      el.progress.hidden = true;
      el.emptyTitle.textContent = "No launches in this window.";
      el.emptyBody.textContent = job.message || "Try a wider timeframe.";
      return;
    }
    if (results.length === 0) { el.msg.textContent = job.message || "Working…"; return; }

    state.rows = results;
    el.section.hidden = false;
    el.filters.hidden = false;
    rebuildTopicOptions();
    paint();

    const pct = job.total ? Math.round((job.enriched / job.total) * 92) + 8 : 10;
    el.fill.style.width = `${Math.min(pct, 100)}%`;
    el.msg.textContent = job.state === "enriching"
      ? `Enriching ${job.enriched}/${job.total}…`
      : job.message || "Working…";
  }
})();
