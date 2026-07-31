import * as React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.js";

const container = document.getElementById("root");
if (!container) throw new Error("no #root");
createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
document.body.dataset.appReady = "true";
