import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app";
import { ThemeProvider } from "@/lib/theme";
import "@/index.css";

const root = document.getElementById("root");
if (!root) throw new Error("index.html is missing #root");

createRoot(root).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
);
