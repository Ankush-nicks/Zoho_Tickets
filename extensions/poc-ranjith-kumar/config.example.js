// One extension = one POC = one baked-in token. Rotate by regenerating the
// token value in the server's .env (POC_TOKEN_RANJITH_KUMAR) and pasting the
// same value here, then reloading the unpacked extension in chrome://extensions.
const POC_CONFIG = {
  apiBaseUrl: "http://localhost:8000",
  token: "REPLACE_WITH_THE_SAME_VALUE_AS_POC_TOKEN_RANJITH_KUMAR_IN_THE_SERVER_ENV",
  pocDisplayName: "Ranjith Kumar",
};
