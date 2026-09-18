import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.js";

const root = document.getElementById("root");
// Not a silent `if (!root) return`. `renderInvoiceBar` in the Python app was complete, had five
// passing tests, and was INVISIBLE in the browser because its target div did not exist and its own
// guard made that silent. A missing mount point is a build error, so it should say so.
if (!root) throw new Error("#root is missing from index.html");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
