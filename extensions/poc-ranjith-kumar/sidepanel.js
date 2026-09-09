const state = {
  tickets: [],
  summary: null,
  sort: "risk",
  filter: null,
  expandedId: null,
  showOlder: false,
  error: null,
  isRetryable: false,
};

const PRIORITY_ORDER = { P1: 0, P2: 1, P3: 2, P4: 3 };

// Keys match each tile's data-filter attribute (set in renderSummary) and
// the same predicates the backend used to compute summary.breached /
// needs_ack_now / on_track, so a tile's count always matches what clicking
// it reveals.
const TILE_FILTERS = {
  breached: (t) => t.sla_state === "breached",
  "needs-ack": (t) => t.ack_state === "missed" || t.ack_urgent,
  "on-track": (t) => t.sla_state === "on_track",
};

// Tickets raised within this many days are shown by default; older ones
// are hidden behind the "Show older tickets" toggle so the list stays
// focused on what's new instead of the full backlog.
const RECENT_DAYS = 3;

function formatDuration(totalSeconds) {
  const abs = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(abs / 3600);
  const minutes = Math.floor((abs % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function sortTickets(tickets, mode, now) {
  const copy = tickets.slice();
  if (mode === "priority") {
    copy.sort((a, b) => (PRIORITY_ORDER[a.priority] ?? 99) - (PRIORITY_ORDER[b.priority] ?? 99));
    return copy;
  }
  if (mode === "sla") {
    copy.sort((a, b) => (a.sla_deadline_at - now) - (b.sla_deadline_at - now));
    return copy;
  }
  // "risk" (default): breached first, then ack missed, then ack urgent,
  // then the rest ordered by soonest-to-breach.
  const rank = (t) => {
    if (t.sla_state === "breached") return 0;
    if (t.ack_state === "missed") return 1;
    if (t.ack_urgent) return 2;
    return 3;
  };
  copy.sort((a, b) => {
    const diff = rank(a) - rank(b);
    if (diff !== 0) return diff;
    return (a.sla_deadline_at - now) - (b.sla_deadline_at - now);
  });
  return copy;
}

function ackLine(ticket, now) {
  if (ticket.ack_state === "acknowledged") {
    return {
      text: `✓ Acknowledged · ${formatDuration(ticket.acknowledged_at - ticket.created_at)} after raise`,
      cls: "ok",
    };
  }
  if (ticket.ack_state === "missed") {
    return {
      text: `! Ack window missed · ${formatDuration(now - ticket.ack_deadline_at)} overdue`,
      cls: "bad",
    };
  }
  const icon = ticket.ack_urgent ? "⏰" : "•";
  return {
    text: `${icon} Ack due in ${formatDuration(ticket.ack_deadline_at - now)}`,
    cls: ticket.ack_urgent ? "warn" : "neutral",
  };
}

function slaLine(ticket, now) {
  if (ticket.sla_state === "breached") {
    return { text: `SLA breached · ${formatDuration(ticket.sla_overdue_seconds)} overdue`, cls: "bad" };
  }
  if (ticket.sla_state === "at_risk") {
    return { text: `SLA at risk · ${formatDuration(ticket.sla_deadline_at - now)} left`, cls: "warn" };
  }
  return { text: `SLA on track · ${formatDuration(ticket.sla_deadline_at - now)} left`, cls: "ok" };
}

// Drives the collapsed ticket-id color, the card's left-border accent, and
// the progress bar fill color - the same three buckets the summary tiles
// use (breached / needs-ack / on-track), so a collapsed card's color always
// means the same thing as the tile it would count under.
function severity(ticket) {
  if (ticket.sla_state === "breached") return "critical";
  if (ticket.sla_state === "at_risk" || ticket.ack_state === "missed" || ticket.ack_urgent) return "warning";
  return "good";
}

function progressFraction(ticket, now) {
  const total = ticket.sla_deadline_at - ticket.created_at;
  if (total <= 0) return 1;
  const elapsed = now - ticket.created_at;
  return Math.max(0, Math.min(1, elapsed / total));
}

function renderSummary(summary) {
  const active = (key) => (state.filter === key ? " active" : "");
  document.getElementById("summary").innerHTML = `
    <div class="summary-stat breached${active("breached")}" data-filter="breached"><span class="count">${summary.breached}</span><span class="label">Out of SLA</span></div>
    <div class="summary-stat needs-ack${active("needs-ack")}" data-filter="needs-ack"><span class="count">${summary.needs_ack_now}</span><span class="label">Need ack now</span></div>
    <div class="summary-stat on-track${active("on-track")}" data-filter="on-track"><span class="count">${summary.on_track}</span><span class="label">On track</span></div>
  `;
}

function renderHeaderMeta() {
  const now = new Date();
  document.getElementById("asOf").textContent =
    `as of ${now.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} · ` +
    now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

  document.getElementById("pocName").textContent = POC_CONFIG.pocDisplayName || "";
  const team = state.tickets.find((t) => t.assigned_team)?.assigned_team;
  document.getElementById("pocTeam").textContent = team || "";
  document.getElementById("pocCount").textContent =
    state.tickets.length > 0 ? `${state.tickets.length} assigned` : "";
}

function renderCollapsedCard(ticket) {
  const sev = severity(ticket);
  return `
    <div class="ticket-card collapsed" data-id="${ticket.id}">
      <div class="ticket-card-header" data-id="${ticket.id}">
        <div class="header-left">
          <span class="ticket-id sev-${sev}">#${ticket.zoho_ticket_id || ticket.id}</span>
        </div>
        <span class="chevron">›</span>
      </div>
    </div>
  `;
}

function renderExpandedCard(ticket, now) {
  const ack = ackLine(ticket, now);
  const sla = slaLine(ticket, now);
  const sev = severity(ticket);
  const fraction = Math.round(progressFraction(ticket, now) * 100);
  const priorityHtml = ticket.priority ? `<span class="priority-pill">${ticket.priority}</span>` : "";
  const issueHtml = ticket.issue_summary
    ? `<div class="issue-summary">${ticket.issue_summary}</div>`
    : "";
  return `
    <div class="ticket-card severity-${sev} expanded" data-id="${ticket.id}">
      <div class="ticket-card-header" data-id="${ticket.id}">
        <div class="header-left">
          <span class="ticket-id sev-${sev}">#${ticket.zoho_ticket_id || ticket.id}</span>
          ${priorityHtml}
        </div>
        <span class="chevron open">›</span>
      </div>
      <div class="ticket-category">
        <span class="category-pill">${ticket.category_group_code}</span>
        <span class="category-name">${ticket.category_group_name}</span>
      </div>
      ${issueHtml}
      <div class="pill-row">
        <div class="pill ${ack.cls}">${ack.text}</div>
      </div>
      <div class="sla-line">
        <span class="pill ${sla.cls}">${sla.text}</span>
        <span class="sla-hours-total">${ticket.sla_hours}h total</span>
      </div>
      <div class="progress-track"><div class="progress-fill ${sla.cls}" style="width:${fraction}%"></div></div>
      <div class="card-footer">
        <button class="open-in-zoho" disabled title="Not available yet - no Zoho record URL configured">Open in Zoho ↗</button>
      </div>
    </div>
  `;
}

function renderTicketCard(ticket, now) {
  return ticket.id === state.expandedId ? renderExpandedCard(ticket, now) : renderCollapsedCard(ticket);
}

function render() {
  if (!state.error && !state.summary) return; // still loading, nothing to render yet

  const now = Date.now() / 1000;
  const listEl = document.getElementById("list");

  if (state.error) {
    document.getElementById("summary").innerHTML = "";
    const retryButton = state.isRetryable ? `<button class="retry-btn" id="retry-btn">Retry</button>` : "";
    listEl.innerHTML = `<div class="empty-state error">${state.error}${retryButton}</div>`;
    if (state.isRetryable) {
      document.getElementById("retry-btn").addEventListener("click", load);
    }
    return;
  }

  renderSummary(state.summary);
  renderHeaderMeta();
  setupSummaryClicks();

  if (state.tickets.length === 0) {
    listEl.innerHTML = `<div class="empty-state">Queue is empty — nothing open right now.</div>`;
    return;
  }

  const filtered = state.filter ? state.tickets.filter(TILE_FILTERS[state.filter]) : state.tickets;
  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="empty-state">No tickets in this filter — click the tile again to show all.</div>`;
    return;
  }

  const sorted = sortTickets(filtered, state.sort, now);
  const recentCutoff = now - RECENT_DAYS * 24 * 3600;
  const recent = sorted.filter((t) => t.created_at >= recentCutoff);
  const older = sorted.filter((t) => t.created_at < recentCutoff);

  // If every ticket in this filter happens to be "old", showing an empty
  // recent list plus a toggle would just be confusing - show them all
  // directly instead of forcing an extra click.
  const recentHtml = recent.length > 0
    ? recent.map((t) => renderTicketCard(t, now)).join("")
    : older.map((t) => renderTicketCard(t, now)).join("");

  let olderHtml = "";
  if (recent.length > 0 && older.length > 0) {
    const label = state.showOlder
      ? "Hide older tickets"
      : `Show ${older.length} older ticket${older.length === 1 ? "" : "s"}`;
    olderHtml = `<button class="show-older-btn">${label}</button>`;
    if (state.showOlder) {
      olderHtml += older.map((t) => renderTicketCard(t, now)).join("");
    }
  }

  listEl.innerHTML = recentHtml + olderHtml;
}

function setupSummaryClicks() {
  document.querySelectorAll(".summary-stat").forEach((tile) => {
    tile.addEventListener("click", () => {
      const key = tile.dataset.filter;
      state.filter = state.filter === key ? null : key;
      state.expandedId = null;
      state.showOlder = false;
      render();
    });
  });
}

function setupTabs() {
  document.querySelectorAll(".sort-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sort-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.sort = btn.dataset.sort;
      render();
    });
  });
}

// Delegated once on the (persistent) list container, since its children are
// fully replaced every render() - re-attaching per-card listeners on every
// render would still work but this is simpler and never leaks listeners.
function setupListDelegation() {
  document.getElementById("list").addEventListener("click", (event) => {
    const showOlderBtn = event.target.closest(".show-older-btn");
    if (showOlderBtn) {
      state.showOlder = !state.showOlder;
      render();
      return;
    }
    const header = event.target.closest(".ticket-card-header");
    if (header) {
      const id = header.dataset.id;
      state.expandedId = state.expandedId === id ? null : id;
      render();
    }
  });
}

async function load() {
  if (!POC_CONFIG.token || POC_CONFIG.token.startsWith("REPLACE_WITH")) {
    state.error = "This extension isn't configured yet — set a real token in config.js.";
    state.isRetryable = false;
    render();
    return;
  }
  try {
    const response = await fetch(`${POC_CONFIG.apiBaseUrl}/api/extension/my-tickets`, {
      headers: { "X-POC-Token": POC_CONFIG.token },
    });
    if (!response.ok) {
      state.error = `Server returned ${response.status}. Check the token and server URL in config.js.`;
      state.isRetryable = true;
      render();
      return;
    }
    const data = await response.json();
    state.tickets = data.tickets;
    state.summary = data.summary;
    state.error = null;
    state.isRetryable = false;
    render();
  } catch (err) {
    state.error = "Couldn't reach the server. Is it running, and is the URL in config.js correct?";
    state.isRetryable = true;
    render();
  }
}

setupTabs();
setupListDelegation();
load();
