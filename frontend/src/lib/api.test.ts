import { describe, expect, it, vi } from "vitest";

import { ApiError, fetchAudit, fetchCatalog, fetchHealth } from "@/lib/api";
import { auditSchema, catalogSchema, healthSchema } from "@/lib/schemas";
import { audit, catalog, health, mockApi } from "@/test/fixtures";

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("API payload parsing", () => {
  it("accepts the three real payloads", () => {
    expect(healthSchema.parse(health)).toEqual(health);
    expect(catalogSchema.parse(catalog).items).toHaveLength(2);
    expect(auditSchema.parse(audit).journals).toHaveLength(2);
  });

  it("keeps the value the API sent instead of coercing one", () => {
    const parsed = catalogSchema.parse(catalog);
    expect(parsed.items[0]?.price_cents).toBe(8900);
    expect(parsed.items[0]?.variants[1]?.available).toBe(0);
    expect(auditSchema.parse(audit).sales[0]?.total_cents).toBe(17800);
  });

  it("rejects a health payload missing a field", () => {
    const { bind: _bind, ...withoutBind } = health;
    expect(healthSchema.safeParse(withoutBind).success).toBe(false);
  });

  it("rejects a non-integer money value", () => {
    const broken = { ...catalog, items: [{ ...catalog.items[0], price_cents: 89.5 }] };
    expect(catalogSchema.safeParse(broken).success).toBe(false);
  });

  it("rejects a nullable field the API always sends", () => {
    const broken = { ...audit, journals: [{ ...audit.journals[0], sale_id: 7 }] };
    expect(auditSchema.safeParse(broken).success).toBe(false);
  });

  it("keeps a null expected_at, which the API does send", () => {
    const withNull = {
      ...catalog,
      purchase_orders: [{ ...catalog.purchase_orders[0], expected_at: null }],
    };
    expect(catalogSchema.parse(withNull).purchase_orders[0]?.expected_at).toBeNull();
  });
});

describe("fetch wrappers", () => {
  it("returns the parsed payload per route", async () => {
    mockApi();
    await expect(fetchHealth()).resolves.toMatchObject({ app_id: "atlas-erp" });
    await expect(fetchCatalog()).resolves.toMatchObject({ source: "db/seed.sql" });
    await expect(fetchAudit()).resolves.toMatchObject({ items: audit.items });
  });

  it("reads only the three read-only routes", async () => {
    const fetchMock = mockApi();
    await fetchHealth();
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/health");
    expect(init.method).toBeUndefined();
    expect(init.body).toBeUndefined();
  });

  it("turns a server error into a readable message", async () => {
    vi.stubGlobal("fetch", async () => response({ error: "internal server error" }, 500));
    await expect(fetchCatalog()).rejects.toBeInstanceOf(ApiError);
  });

  it("turns a refused connection into a readable message", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(fetchHealth()).rejects.toThrow(/Could not reach the ERP console API/);
  });

  it("turns a non-JSON body into a readable message", async () => {
    vi.stubGlobal("fetch", async () => ({ ...response({}), json: async () => {
      throw new SyntaxError("Unexpected token <");
    } } as unknown as Response));
    await expect(fetchAudit()).rejects.toThrow(/did not answer JSON/);
  });

  it("refuses a payload the schema no longer accepts", async () => {
    vi.stubGlobal("fetch", async () => response({ app_id: "atlas-erp" }));
    await expect(fetchHealth()).rejects.toThrow(/unexpected shape/);
  });
});
