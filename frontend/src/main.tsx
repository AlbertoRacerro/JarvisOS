import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { applyStoredVisualPreferences } from "./theme";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/foundation.css";
import "./styles/shell.css";
import "./styles/final-fusion.css";
import "./styles/final-fusion-canonical-overrides.css";
import "./styles/operator-semantic-ux.css";
import "./styles/final-fusion-shell-overrides.css";
import "./styles/final-workspace-header.css";
import "./styles/runs.css";
import "./styles/engineering-data.css";
import "./styles/analytics.css";
import "./styles/review.css";
import "./styles/ai-threads.css";
import "./styles/settings.css";
import "./styles/responsive.css";
import "./styles/100g-ui-repair.css";
import "./styles/operator-recovery.css";
// Spec 198 Lumen composition: last, so the production identity wins over the earlier layered overrides.
import "./styles/lumen.css";

applyStoredVisualPreferences();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
