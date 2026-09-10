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
    // Zoho persists this checkbox's state across page loads - unconditionally
    // clicking it (as an earlier version of this did) toggles it OFF when a
    // prior session had already left it checked, which then breaks every
    // later step. Only click when it's actually unchecked. (Confirmed live:
    // an unconditional click flipped a previously-checked box to
    // `checkboxChecked: false`.)
    if (!ticketIdCheckbox.checked) {
      ticketIdCheckbox.click();
      // Give the widget a moment to finish wiring up the now-visible search
      // field before we touch it - toggling it can leave a brief window
      // where the field exists in the DOM but isn't fully live yet (seen
      // live as a search that returned zero suggestions and fired zero
      // network requests, immediately after a checkbox toggle).
      await new Promise((r) => setTimeout(r, 400));
    }

    const searchInput = await waitFor(
      () => document.querySelector(".select2-search-field input.select2-input"),
      8000
    );
    if (!searchInput) return;

    // This is a multi-select select2 field: it accumulates a chip per
    // selected suggestion rather than replacing the previous one. Any chip
    // left over from an earlier search (a different ticket ID) stays
    // selected and gets OR'd into the search criteria alongside whatever we
    // pick next - confirmed live as a search silently running as
    // "Ticket ID is either '2698' or '2606'" using stale IDs from an
    // entirely earlier attempt. Clear every existing chip first, scoped to
    // this field's own chip list (not the whole page), removing each one
    // the same way select2 expects a real removal click: a full
    // mousedown/mouseup/click sequence on its "x" close control, not
    // .click() (which never fires mousedown/mouseup - the same reason plain
    // .click() didn't work for selecting a dropdown option either).
    const choicesList = searchInput.closest("ul.select2-choices");
    if (choicesList) {
      const existingChips = Array.from(choicesList.querySelectorAll(".select2-search-choice"));
      for (const chip of existingChips) {
        const closeBtn = chip.querySelector(".select2-search-choice-close") || chip;
        const rect = closeBtn.getBoundingClientRect();
        const chipMouseProps = {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: rect.left + 2,
          clientY: rect.top + 2,
          button: 0,
          buttons: 1,
          which: 1,
        };
        closeBtn.dispatchEvent(new MouseEvent("mousedown", chipMouseProps));
        closeBtn.dispatchEvent(new MouseEvent("mouseup", chipMouseProps));
        closeBtn.dispatchEvent(new MouseEvent("click", chipMouseProps));
      }
      if (existingChips.length > 0) {
        await new Promise((r) => setTimeout(r, 200));
      }
    }

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
    function typeTicketId() {
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
    }

    typeTicketId();

    // Ticket ID is a select2 autocomplete field, not free text: typing
    // opens a dropdown of matching suggestions fetched via an async
    // (likely AJAX-backed) search, and the value only "counts" as a real
    // search criterion once a suggestion is clicked - the Search button
    // ignores whatever is still just sitting in the text box otherwise.
    //
    // The naive version of this step (grab whatever <li> exists the
    // moment the dropdown appears) is a real race: select2 renders a
    // "Searching…" placeholder immediately, then replaces it with the
    // real match(es) once the request resolves - reading too early clicks
    // the placeholder, not the actual ticket, which is exactly the "works
    // on the 2nd or 3rd click" symptom (the request has often already
    // finished/cached by a later attempt). waitForSettledDropdownOption
    // instead requires: no "searching" indicator active, AND the same
    // candidate option text observed continuously for a short window -
    // i.e. the list has actually stopped changing, not just "has an item
    // right now".
    async function waitForSettledDropdownOption(timeoutMs) {
      const start = Date.now();
      let lastText = null;
      let stableSince = null;
      const isPlaceholder = (text) => /searching|no matches|loading/i.test(text);

      while (Date.now() - start < timeoutMs) {
        const stillSearching = document.querySelector(
          ".select2-drop-active.select2-searching, .select2-drop.select2-searching, .select2-active"
        );
        if (!stillSearching) {
          const options = Array.from(
            document.querySelectorAll(
              ".select2-drop-active .select2-results li, .select2-drop .select2-results li, .select2-results li"
            )
          ).filter((opt) => {
            const text = (opt.textContent || "").trim();
            return text && !isPlaceholder(text);
          });

          const exact = options.find((opt) => opt.textContent.trim() === String(ticketId));
          const partial = options.find((opt) => opt.textContent.includes(String(ticketId)));
          const candidate = exact || partial || options[0] || null;

          if (candidate) {
            const text = candidate.textContent.trim();
            if (text === lastText && stableSince && Date.now() - stableSince > 350) {
              return candidate;
            }
            if (text !== lastText) {
              lastText = text;
              stableSince = Date.now();
            }
          } else {
            lastText = null;
            stableSince = null;
          }
        }
        await new Promise((r) => setTimeout(r, 150));
      }
      return null;
    }

    // Even with the checkbox-ready delay above, the widget occasionally
    // isn't actually listening yet right after the "Ticket ID" checkbox is
    // toggled on - confirmed live: the exact same ticket ID that returned
    // zero suggestions and fired zero network requests on one attempt
    // worked reliably moments later with no other change. A bare settle
    // delay didn't fully close this window, so retype and wait again once
    // rather than give up after a single silent failure.
    let dropdownOption = await waitForSettledDropdownOption(8000);
    if (!dropdownOption) {
      typeTicketId();
      dropdownOption = await waitForSettledDropdownOption(8000);
    }
    if (dropdownOption) {
      // Confirmed live against the real portal: select2 v3 binds selection
      // to a mousedown/mouseup/click sequence on the result's inner
      // .select2-result-label (not a plain "click" on the <li>, and NOT
      // satisfied by calling .click() - HTMLElement.click() only ever
      // fires a "click" event, never mousedown/mouseup, so select2 never
      // saw a selection attempt at all). Dispatching the full sequence
      // with real MouseEvent properties (button/buttons/which/clientX/Y -
      // jQuery's event normalization reads these) on the label reproduces
      // exactly what a genuine click does, without needing OS-level input.
      const target = dropdownOption.querySelector(".select2-result-label") || dropdownOption;
      const rect = target.getBoundingClientRect();
      const mouseEventProps = {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: rect.left + 5,
        clientY: rect.top + 5,
        button: 0,
        buttons: 1,
        which: 1,
      };
      target.dispatchEvent(new MouseEvent("mousedown", mouseEventProps));
      target.dispatchEvent(new MouseEvent("mouseup", mouseEventProps));
      target.dispatchEvent(new MouseEvent("click", mouseEventProps));
      // Give select2 a moment to actually commit the selection into its
      // internal state before touching the Search button - clicking an
      // option and immediately clicking Search is its own smaller race.
      await new Promise((r) => setTimeout(r, 500));
    }

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
