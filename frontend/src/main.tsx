import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { applyTheme, readStoredTheme } from "./stores/themeStore";
import "./styles.css";
import "./styles/broadsheet.css";

// Before React mounts: the class has to be on <html> for the first paint, or
// a saved Broadsheet choice shows a frame of the dark theme on every load.
applyTheme(readStoredTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
