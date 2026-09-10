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

    // select2-style widgets generally don't react to a bulk `.value = X`
    // assignment plus one synthetic event - they're built to react to
    // genuine typing (per-character key events, or a real insertText
    // input event), not a property write. Click first (many widgets only
    // "arm" their input handling after a real pointer event, not just
    // .focus()), then try document.execCommand("insertText", ...), which
    // Chromium still supports and which fires a real `input` event with
    // inputType "insertText" - closer to actual typing than any purely
    // synthetic event. Fall back to a manual per-character keydown/input/
    // keyup sequence if execCommand didn't actually land the text (e.g.
    // if it's unsupported or the widget still ignored it).
    searchInput.click();
    searchInput.focus();
    searchInput.value = "";

    const usedExecCommand =
      document.execCommand && document.execCommand("insertText", false, ticketId);

    if (!usedExecCommand || searchInput.value !== ticketId) {
      searchInput.value = "";
      for (const ch of String(ticketId)) {
        searchInput.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true }));
        searchInput.value += ch;
        searchInput.dispatchEvent(new Event("input", { bubbles: true }));
        searchInput.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
      }
    }
    searchInput.dispatchEvent(new Event("change", { bubbles: true }));

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

function runZohoSearch(tabId, ticketId) {
  if (!ticketId) return; // no ticket id to search for - just leave the report open
  chrome.scripting.executeScript({
    target: { tabId },
    func: zohoSearchForTicketId,
    args: [ticketId],
  });
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== "openZohoTicketSearch") return;

  // Reuse an already-open Zoho tab instead of opening a new one every
  // click. Match by origin only (not the full URL with its #Report:...
  // fragment) - match patterns don't consider the fragment anyway, and
  // any tab already on this Zoho app is the right one to reuse regardless
  // of which report/record it's currently showing.
  const zohoOrigin = new URL(message.zohoReportUrl).origin + "/*";

  chrome.tabs.query({ url: zohoOrigin }, (existingTabs) => {
    const existing = existingTabs[0];

    if (existing) {
      chrome.windows.update(existing.windowId, { focused: true });
      chrome.tabs.update(existing.id, { active: true, url: message.zohoReportUrl });

      let injected = false;
      const injectOnce = () => {
        if (injected) return;
        injected = true;
        chrome.tabs.onUpdated.removeListener(onUpdated);
        runZohoSearch(existing.id, message.ticketId);
      };
      function onUpdated(updatedTabId, changeInfo) {
        if (updatedTabId !== existing.id || changeInfo.status !== "complete") return;
        injectOnce();
      }
      chrome.tabs.onUpdated.addListener(onUpdated);
      // Re-pointing a tab at the exact URL it's already on (same fragment
      // included) typically doesn't fire a real navigation/onUpdated event
      // at all - don't wait forever for something that may never happen.
      setTimeout(injectOnce, 1200);
      return;
    }

    chrome.tabs.create({ url: message.zohoReportUrl }, (tab) => {
      if (!tab.id) return;
      const tabId = tab.id;
      function onNewTabUpdated(updatedTabId, changeInfo) {
        if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
        chrome.tabs.onUpdated.removeListener(onNewTabUpdated);
        runZohoSearch(tabId, message.ticketId);
      }
      chrome.tabs.onUpdated.addListener(onNewTabUpdated);
    });
  });
});
