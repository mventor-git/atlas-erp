import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// `globals: false` means Testing Library cannot auto-register its cleanup, so
// the DOM is torn down here once for the whole suite.
afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});
