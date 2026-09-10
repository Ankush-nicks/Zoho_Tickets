// Only reason this exists: chrome.sidePanel requires a service worker to
// open the panel when the toolbar icon is clicked (there is no declarative
// manifest equivalent of action.default_popup for the side panel).
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

// Runs INSIDE the Zoho Creator report tab (injected via
// chrome.scripting.executeScript) - must be fully self-contained, no
// closures over this file's own scope, since it's serialized and executed
// in the target page's context.
//
// Zoho Creator is a heavy jQuery/select2 SPA: the tab's "complete" load
// status only means the initial document loaded, not that the search UI
// has finished rendering. Every step here polls for its target element
// rather than assuming a fixed delay, and gives up quietly (no visible
// error) if a step's element never appears within its own timeout - this
// was built from a markup snippet, not verified against the live,
// authenticated portal, so failing silently is safer than a broken/partial
// click sequence leaving the page in a confusing half-state.
function zohoSearchForTicketId(ticketId) {
  function waitFor(findFn, timeoutMs) {
    return new Promise((resolve) => {
      const start = Date.now();
      (function tick() {
        const el = findFn();
        if (el) return resolve(el);
        if (Date.now() - start > timeoutMs) return resolve(null);
        setTimeout(tick, 150);
      })();
    });
  }

  (async () => {
    const searchToggle = await waitFor(
      () => document.querySelector('a[elname="zc-showSearchDivEl"]'),
      8000
    );
    if (!searchToggle) return;
    searchToggle.click();

    const ticketIdCheckbox = await waitFor(() => {
      const boxes = document.querySelectorAll('[elname="zc-searchCheckBoxEl"]');
      for (const box of boxes) {
        if (box.textContent && box.textContent.includes("Ticket ID")) {
          // Click the real checkbox input directly, not one of its two
          // <label for> elements (one is a visually-hidden a11y label with
          // no text, the other carries the visible "Ticket ID" text) -
          // this works regardless of which label the custom-checkbox CSS
          // actually renders.
          return box.querySelector('input[type="checkbox"]') || box;
        }
      }
      return null;
    }, 8000);
    if (!ticketIdCheckbox) return;
    ticketIdCheckbox.click();

    const searchInput = await waitFor(
      () => document.querySelector(".select2-search-field input.select2-input"),
      8000
    );
    if (!searchInput) return;
    searchInput.focus();
    searchInput.value = ticketId;
    searchInput.dispatchEvent(new Event("input", { bubbles: true }));
    searchInput.dispatchEvent(new Event("change", { bubbles: true }));
    searchInput.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));

    // The search button is already in the DOM at this point (same static
    // panel), so waitFor below would resolve immediately - this grace
    // delay is for Zoho's own debounced input-processing, not for the
    // button's existence.
    await new Promise((r) => setTimeout(r, 400));

    const searchButton = await waitFor(
      () => document.querySelector('a[elname="zc-advSearchReportEl"]'),
      8000
    );
    if (!searchButton) return;
    searchButton.click();
  })();
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== "openZohoTicketSearch") return;

  chrome.tabs.create({ url: message.zohoReportUrl }, (tab) => {
    if (!message.ticketId || !tab.id) return; // no ticket id to search for - just leave the report open

    const tabId = tab.id;
    function onUpdated(updatedTabId, changeInfo) {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.scripting.executeScript({
        target: { tabId },
        func: zohoSearchForTicketId,
        args: [message.ticketId],
      });
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
});
