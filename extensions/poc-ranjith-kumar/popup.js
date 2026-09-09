const state = { tickets: [], summary: null, sort: "risk", error: null, isRetryable: false };

const PRIORITY_ORDER = { P1: 0, P2: 1, P3: 2, P4: 3 };

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
      text: `Acknowledged · ${formatDuration(ticket.acknowledged_at - ticket.created_at)} after raise`,
      cls: "ok",
    };
  }
  if (ticket.ack_state === "missed") {
    return {
      text: `Ack window missed · ${formatDuration(now - ticket.ack_deadline_at)} overdue`,
      cls: "bad",
    };
  }
  return {
    text: `Ack due in ${formatDuration(ticket.ack_deadline_at - now)}`,
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
  return { text: `On track · ${formatDuration(ticket.sla_deadline_at - now)} left`, cls: "ok" };
}

function renderSummary(summary) {
  document.getElementById("summary").innerHTML = `
    <div class="summary-stat breached"><span class="count">${summary.breached}</span><span class="label">Out of SLA</span></div>
    <div class="summary-stat needs-ack"><span class="count">${summary.needs_ack_now}</span><span class="label">Needs ack now</span></div>
    <div class="summary-stat on-track"><span class="count">${summary.on_track}</span><span class="label">On track</span></div>
  `;
}

function renderTicketCard(ticket, now) {
  const ack = ackLine(ticket, now);
  const sla = slaLine(ticket, now);
  const priorityHtml = ticket.priority ? `<span class="priority-pill">${ticket.priority}</span>` : "";
  return `
    <div class="ticket-card">
      <div class="ticket-card-top">
        <span class="ticket-id">${ticket.zoho_ticket_id || ticket.id}</span>
        ${priorityHtml}
      </div>
      <div class="ticket-category">${ticket.category_group_code} · ${ticket.category_group_name}</div>
      <div class="pill ${ack.cls}">${ack.text}</div>
      <div class="pill ${sla.cls}">${sla.text}</div>
    </div>
  `;
}

function render() {
  if (!state.error && !state.summary) return; // still loading, nothing to render yet

  const now = Date.now() / 1000;
  const listEl = document.getElementById("list");

  if (state.error) {
    document.getElementById("summary").innerHTML = "";
    const retryButton = state.isRetryable ? `<button id="retry-btn" style="margin-top: 12px; padding: 8px 16px; background: #1a1a1a; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 12px;">Retry</button>` : "";
    listEl.innerHTML = `<div class="empty-state error">${state.error}${retryButton}</div>`;
    if (state.isRetryable) {
      document.getElementById("retry-btn").addEventListener("click", load);
    }
    return;
  }

  renderSummary(state.summary);

  if (state.tickets.length === 0) {
    listEl.innerHTML = `<div class="empty-state">Queue is empty — nothing open right now.</div>`;
    return;
  }

  const sorted = sortTickets(state.tickets, state.sort, now);
  listEl.innerHTML = sorted.map((t) => renderTicketCard(t, now)).join("");
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
load();
