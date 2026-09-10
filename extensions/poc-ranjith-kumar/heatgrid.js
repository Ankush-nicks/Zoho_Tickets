// Subcategory heat grid: which of this POC's subcategories currently have
// the most open tickets piling up. Self-contained (own state, own fetch,
// own render loop) - the only thing it shares with sidepanel.js is
// POC_CONFIG (from config.js) and the view-tab show/hide wiring in
// sidepanel.js's setupViewTabs().

const heatState = {
  subcategories: [], // raw entries from the API, one per owned subcategory
  selectedCategory: null, // category_code
  expandedCode: null, // subcategory_code currently showing its detail panel
  detailShowAll: false, // false = capped ticket list, true = every ticket
  error: null,
  isRetryable: false,
};

// Tickets shown in the drill-down panel before the "+N more" cap kicks in.
// Not user-tunable like the tier thresholds - this is a display density
// choice, not a business threshold.
const HEAT_CHIP_CAP = 20;

function heatTierFor(count) {
  const t = POC_CONFIG.heatTierThresholds || { medium: 30, high: 50 };
  if (count >= t.high) return "high";
  if (count >= t.medium) return "medium";
  return "low";
}

function heatCategories() {
  const seen = new Map();
  heatState.subcategories.forEach((s) => {
    if (!seen.has(s.category_code)) {
      seen.set(s.category_code, s.category_name);
    }
  });
  return Array.from(seen, ([code, name]) => ({ code, name }));
}

function renderHeatLegend() {
  const t = POC_CONFIG.heatTierThresholds || { medium: 30, high: 50 };
  document.getElementById("heatLegend").innerHTML = `
    <span class="legend-item"><span class="legend-swatch tier-low"></span>Low (&lt;${t.medium})</span>
    <span class="legend-item"><span class="legend-swatch tier-medium"></span>Medium (${t.medium}–${t.high - 1})</span>
    <span class="legend-item"><span class="legend-swatch tier-high"></span>High (${t.high}+) ▲</span>
  `;
}

function renderCategorySelect() {
  const categories = heatCategories();
  const select = document.getElementById("categorySelect");
  select.innerHTML = categories
    .map((c) => `<option value="${c.code}">${c.code} · ${c.name}</option>`)
    .join("");
  select.value = heatState.selectedCategory;
}

function renderCategoryTotal() {
  const inCategory = heatState.subcategories.filter((s) => s.category_code === heatState.selectedCategory);
  const total = inCategory.reduce((sum, s) => sum + s.open_count, 0);
  document.getElementById("categoryTotal").textContent = `${total} open total`;
}

function renderHeatTile(sub) {
  const tier = heatTierFor(sub.open_count);
  const badge = tier === "high" ? `<div class="heat-badge">▲ High volume</div>` : "";
  return `
    <div class="heat-tile tier-${tier}" data-code="${sub.subcategory_code}">
      <div class="heat-tile-count">${sub.open_count}</div>
      <div class="heat-tile-code">${sub.subcategory_code}</div>
      <div class="heat-tile-name">${sub.subcategory_name}</div>
      ${badge}
    </div>
  `;
}

function renderHeatGrid() {
  const inCategory = heatState.subcategories
    .filter((s) => s.category_code === heatState.selectedCategory)
    .slice()
    .sort((a, b) => b.open_count - a.open_count);

  document.getElementById("heatGrid").innerHTML = inCategory.map(renderHeatTile).join("");
}

function renderHeatDetail() {
  const detailEl = document.getElementById("heatDetail");
  if (!heatState.expandedCode) {
    detailEl.innerHTML = "";
    return;
  }
  const sub = heatState.subcategories.find((s) => s.subcategory_code === heatState.expandedCode);
  if (!sub) {
    detailEl.innerHTML = "";
    return;
  }

  if (sub.tickets.length === 0) {
    detailEl.innerHTML = `
      <div class="heat-detail-header">${sub.subcategory_code} · ${sub.subcategory_name}</div>
      <div class="empty-state">No open tickets in this subcategory right now.</div>
    `;
    return;
  }

  const visible = heatState.detailShowAll ? sub.tickets : sub.tickets.slice(0, HEAT_CHIP_CAP);
  const remaining = sub.tickets.length - visible.length;
  const chips = visible
    .map((t) => `<button class="ticket-chip" data-zoho-id="${t.zoho_ticket_id || ""}" title="Opens the Assigned Tickets report in Zoho and searches for this ticket ID">#${t.zoho_ticket_id || t.id} ↗</button>`)
    .join("");
  const moreBtn = remaining > 0
    ? `<button class="show-more-chips-btn">+${remaining} more</button>`
    : (heatState.detailShowAll && sub.tickets.length > HEAT_CHIP_CAP
        ? `<button class="show-more-chips-btn">Show fewer</button>`
        : "");

  detailEl.innerHTML = `
    <div class="heat-detail-header">${sub.subcategory_code} · ${sub.subcategory_name} — ${sub.open_count} open</div>
    <div class="chip-row">${chips}${moreBtn}</div>
  `;
}

function renderHeatAll() {
  if (heatState.error) {
    document.getElementById("heatGrid").innerHTML = "";
    document.getElementById("heatDetail").innerHTML = "";
    document.getElementById("heatLegend").innerHTML = "";
    const retryButton = heatState.isRetryable ? `<button class="retry-btn" id="heatRetryBtn">Retry</button>` : "";
    document.getElementById("categoryTotal").textContent = "";
    document.getElementById("categorySelect").innerHTML = "";
    document.getElementById("heatGrid").innerHTML = `<div class="empty-state error">${heatState.error}${retryButton}</div>`;
    if (heatState.isRetryable) {
      document.getElementById("heatRetryBtn").addEventListener("click", loadHeatGrid);
    }
    return;
  }

  if (heatState.subcategories.length === 0) {
    document.getElementById("heatLegend").innerHTML = "";
    document.getElementById("categorySelect").innerHTML = "";
    document.getElementById("categoryTotal").textContent = "";
    document.getElementById("heatGrid").innerHTML = `<div class="empty-state">No subcategories are routed to you yet.</div>`;
    return;
  }

  renderHeatLegend();
  renderCategorySelect();
  renderCategoryTotal();
  renderHeatGrid();
  renderHeatDetail();
}

function setupHeatControls() {
  document.getElementById("categorySelect").addEventListener("change", (event) => {
    heatState.selectedCategory = event.target.value;
    heatState.expandedCode = null;
    heatState.detailShowAll = false;
    renderHeatAll();
  });

  document.getElementById("heatGrid").addEventListener("click", (event) => {
    const tile = event.target.closest(".heat-tile");
    if (!tile) return;
    const code = tile.dataset.code;
    heatState.expandedCode = heatState.expandedCode === code ? null : code;
    heatState.detailShowAll = false;
    renderHeatDetail();
  });

  document.getElementById("heatDetail").addEventListener("click", (event) => {
    const chip = event.target.closest(".ticket-chip");
    if (chip) {
      openZohoTicketSearch(chip.dataset.zohoId);
      return;
    }
    if (event.target.closest(".show-more-chips-btn")) {
      heatState.detailShowAll = !heatState.detailShowAll;
      renderHeatDetail();
    }
  });
}

async function loadHeatGrid() {
  if (!POC_CONFIG.token || POC_CONFIG.token.startsWith("REPLACE_WITH")) {
    heatState.error = "This extension isn't configured yet — set a real token in config.js.";
    heatState.isRetryable = false;
    renderHeatAll();
    return;
  }
  try {
    const response = await fetch(`${POC_CONFIG.apiBaseUrl}/api/extension/my-subcategory-heat`, {
      headers: { "X-POC-Token": POC_CONFIG.token },
    });
    if (!response.ok) {
      heatState.error = `Server returned ${response.status}. Check the token and server URL in config.js.`;
      heatState.isRetryable = true;
      renderHeatAll();
      return;
    }
    const data = await response.json();
    heatState.subcategories = data.subcategories;
    heatState.error = null;
    heatState.isRetryable = false;
    const categories = heatCategories();
    if (!heatState.selectedCategory && categories.length > 0) {
      heatState.selectedCategory = categories[0].code;
    }
    renderHeatAll();
  } catch (err) {
    heatState.error = "Couldn't reach the server. Is it running, and is the URL in config.js correct?";
    heatState.isRetryable = true;
    renderHeatAll();
  }
}

setupHeatControls();
loadHeatGrid();
