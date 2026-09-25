import { z } from "zod";

import { auditSchema, catalogSchema, healthSchema, type Audit, type Catalog } from "./schemas";

/**
 * The Python API is the only data source. Every route is a GET, so a failure is
 * either the server being down, a non-2xx status, or a payload that no longer
 * matches the schema; all three surface as one readable message plus a retry.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function get<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { accept: "application/json" } });
  } catch {
    // A refused connection is the usual case here: the console server is a
    // separate process the operator may not have started.
    throw new ApiError(
      `Could not reach the ERP console API at ${path}. Is "python -m atlas_erp.web" running?`,
    );
  }
  if (!response.ok) {
    throw new ApiError(`${path} answered ${response.status}.`, response.status);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(`${path} did not answer JSON.`);
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(`${path} returned an unexpected shape: ${parsed.error.message}`);
  }
  return parsed.data;
}

export const fetchHealth = () => get("/api/health", healthSchema);
export const fetchCatalog = (): Promise<Catalog> => get("/api/catalog", catalogSchema);
export const fetchAudit = (): Promise<Audit> => get("/api/audit", auditSchema);
