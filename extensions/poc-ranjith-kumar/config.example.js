// One extension = one POC = one baked-in token. Rotate by regenerating the
// token value in the server's .env (POC_TOKEN_RANJITH_KUMAR) and pasting the
// same value here, then reloading the unpacked extension in chrome://extensions.
const POC_CONFIG = {
  apiBaseUrl: "http://localhost:8000",
  token: "REPLACE_WITH_THE_SAME_VALUE_AS_POC_TOKEN_RANJITH_KUMAR_IN_THE_SERVER_ENV",
  pocDisplayName: "Ranjith Kumar",
  // Heat grid tier thresholds (open-ticket count per subcategory): below
  // `medium` is "low", [medium, high) is "medium", `high` and up is "high".
  // Tune per category/team - these are deliberately not literals in the view code.
  heatTierThresholds: { medium: 30, high: 50 },
  // "Open in Zoho" opens this report (no per-ticket record URL exists yet,
  // so every ticket/chip opens the same Assigned Tickets report - not a
  // deep link to the specific record).
  zohoReportUrl: "https://niat.zohocreatorportal.in/#Report:Assigned_Tickets",
};
