import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConsolePage, NotFound } from "@/app";
import { audit, catalog, health, mockApi, renderConsole } from "@/test/fixtures";

/** Radix activates a tab on mousedown, and only the active panel is mounted. */
function openTab(name: string) {
  fireEvent.mouseDown(screen.getByRole("tab", { name }), { button: 0 });
}

describe("console page", () => {
  it("renders the header, the demo banner, and the health the API reported", async () => {
    mockApi();
    renderConsole(<ConsolePage />);

    expect(screen.getByRole("heading", { level: 1, name: "Atlas ERP console" })).toBeTruthy();
    expect(await screen.findByText(/Demo fixture, in memory\./)).toBeTruthy();
    expect(await screen.findByText(/API ok/)).toBeTruthy();
    expect(screen.getByText("atlas-erp")).toBeTruthy();
    expect(screen.getByText("in-memory")).toBeTruthy();
  });

  it("says the API is unreachable instead of rendering nothing", async () => {
    mockApi({ "/api/health": null, "/api/catalog": null, "/api/audit": null });
    renderConsole(<ConsolePage />);

    expect(await screen.findByText(/API unreachable/)).toBeTruthy();
    // The standing demo banner is a polite status; only the failures alert.
    expect(
      screen.getByText(/Demo fixture, in memory\./).closest('[role="status"]'),
    ).not.toBeNull();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
  });

  it("shows a loading placeholder before the first answer", async () => {
    mockApi();
    renderConsole(<ConsolePage />);

    expect(screen.getAllByText(/^Loading /).length).toBeGreaterThan(0);
    await screen.findByText(/Demo fixture, in memory\./);
  });

  it("renders KPI tiles summed from the payloads", async () => {
    mockApi();
    renderConsole(<ConsolePage />);

    const tiles = await screen.findByRole("list", { name: "Key figures" });
    // 2 products, 3 variants, 61 on hand, 7 reserved, 178.00 of sale revenue.
    expect(within(tiles).getByText("Products").previousElementSibling?.textContent).toBe("2");
    expect(within(tiles).getByText("Variants").previousElementSibling?.textContent).toBe("3");
    expect(within(tiles).getByText("Units on hand").previousElementSibling?.textContent).toBe("61");
    expect(within(tiles).getByText("Units reserved").previousElementSibling?.textContent).toBe("7");
    expect(within(tiles).getByText("Sale revenue").previousElementSibling?.textContent).toBe(
      "178.00",
    );
  });

  it("renders catalogue products with variants, price, availability, and image", async () => {
    mockApi();
    renderConsole(<ConsolePage />);

    const product = await screen.findByRole("heading", { name: "Aurora Everyday Linen Shirt" });
    const card = product.closest("div[data-slot='card']") as HTMLElement;
    expect(within(card).getByText("89.00")).toBeTruthy();
    expect(within(card).getByText("AUR-LIN-SHIRT-SAND-M")).toBeTruthy();
    expect(within(card).getByText("Sand / M")).toBeTruthy();
    expect(within(card).getByText("Out of stock")).toBeTruthy();
    const image = within(card).getAllByRole("img")[0] as HTMLImageElement;
    expect(image.getAttribute("src")).toContain("images.unsplash.com");
    expect(image.getAttribute("alt")).toBe("Aurora Everyday Linen Shirt in Sand, size M");
  });

  it("narrows the catalogue with the filter form", async () => {
    mockApi();
    renderConsole(<ConsolePage />);
    await screen.findByRole("heading", { name: "Aurora Everyday Linen Shirt" });

    const search = screen.getByLabelText("Search") as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(search, "Harbor");
    search.dispatchEvent(new Event("input", { bubbles: true }));

    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Aurora Everyday Linen Shirt" })).toBeNull();
    });
    expect(screen.getByRole("heading", { name: "Harbor Wool Throw" })).toBeTruthy();
  });

  it("reports an empty catalogue result rather than a blank tab", async () => {
    mockApi({ "/api/catalog": { ...catalog, items: [] } });
    renderConsole(<ConsolePage />);

    expect(await screen.findByText(/The API reported no the catalogue\./)).toBeTruthy();
  });

  it("renders purchasing status as a token variant, not as colour", async () => {
    mockApi();
    renderConsole(<ConsolePage />);
    openTab("Purchasing");

    const order = await screen.findByText("PO-2026-0058");
    const row = order.closest("tr") as HTMLElement;
    expect(within(row).getByText("open")).toBeTruthy();
    expect(within(row).getByText("Northport Supply Group")).toBeTruthy();
    expect(within(row).getByText("754.00")).toBeTruthy();
  });

  it("renders the sales and journal rows and flags an unbalanced entry", async () => {
    mockApi();
    renderConsole(<ConsolePage />);
    openTab("Activity");

    const salesTable = await screen.findByRole("table", { name: "Posted manual cash sales." });
    const saleRow = within(salesTable).getByText("sale-2026-0918").closest("tr") as HTMLElement;
    expect(within(saleRow).getByText("Aurora Everyday Linen Shirt x2")).toBeTruthy();
    expect(within(saleRow).getByText("178.00")).toBeTruthy();

    const ledger = screen.getByRole("table", { name: /journal entries/ });
    expect(within(ledger).getByText("balanced")).toBeTruthy();
    expect(within(ledger).getByText("unbalanced")).toBeTruthy();
  });

  it("reports an empty audit result", async () => {
    mockApi({ "/api/audit": { ...audit, sales: [], journals: [] } });
    renderConsole(<ConsolePage />);
    openTab("Activity");

    expect(await screen.findByText(/The API reported no sales and journals\./)).toBeTruthy();
  });

  it("retries a failed route on demand", async () => {
    const fetchMock = mockApi({ "/api/audit": null, "/api/health": health, "/api/catalog": catalog });
    renderConsole(<ConsolePage />);
    const before = fetchMock.mock.calls.length;

    const retry = (await screen.findAllByRole("button", { name: "Retry" }))[0] as HTMLElement;
    retry.click();

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    });
  });
});

describe("unknown route", () => {
  it("says the console has one route and links back to it", () => {
    mockApi();
    renderConsole(<NotFound />);

    expect(screen.getByRole("heading", { name: "No such console page" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Go to the console" })).toBeTruthy();
  });
});

describe("console does not write", () => {
  it("never sends a method other than the default GET", async () => {
    const fetchMock = mockApi();
    renderConsole(<ConsolePage />);
    await screen.findByText(/Demo fixture, in memory\./);

    for (const call of fetchMock.mock.calls) {
      const init = (call[1] ?? {}) as RequestInit;
      expect(init.method ?? "GET").toBe("GET");
      expect(init.body).toBeUndefined();
    }
  });
});
