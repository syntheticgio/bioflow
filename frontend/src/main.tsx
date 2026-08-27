import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installUnloadGuard } from "./stores/uploadStore";
import "./styles.css";
import "./styles/broadsheet.css";

// Module scope, not a component effect: an upload keeps running whether or not
// any particular view is mounted, and StrictMode double-invokes effects. The
// guard attaches itself only while something is actually transferring.
installUnloadGuard();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
