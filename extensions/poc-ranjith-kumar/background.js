// Only reason this exists: chrome.sidePanel requires a service worker to
// open the panel when the toolbar icon is clicked (there is no declarative
// manifest equivalent of action.default_popup for the side panel).
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
