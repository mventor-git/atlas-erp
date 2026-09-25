import { useCallback } from "react";
import { createBrowserRouter, Link, RouterProvider } from "react-router-dom";

import { Alert, AlertDescription, AlertTitle, Separator, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui";
import { ActivityPanel } from "@/components/console/activity-panel";
import { AsyncSection } from "@/components/console/async-section";
import { CatalogPanel } from "@/components/console/catalog-panel";
import { ConsoleHeader } from "@/components/console/console-header";
import { KpiTiles, kpiTiles } from "@/components/console/kpi-tiles";
import { PurchasingPanel } from "@/components/console/purchasing-panel";
import { StockPanel } from "@/components/console/stock-panel";
import { fetchAudit, fetchCatalog, fetchHealth } from "@/lib/api";
import type { Audit, Catalog } from "@/lib/schemas";
import { useResource, type Resource } from "@/lib/use-resource";

/**
 * The KPI tiles need the catalogue and the audit at once. Combining two
 * existing resources keeps one fetch per route and still reuses one loading and
 * error treatment, rather than adding a fourth request.
 */
function overview(
  catalog: Resource<Catalog>,
  audit: Resource<Audit>,
): Resource<{ catalog: Catalog; audit: Audit }> {
  const reload = () => {
    catalog.reload();
    audit.reload();
  };
  if (catalog.status === "error") return { status: "error", data: null, error: catalog.error, reload };
  if (audit.status === "error") return { status: "error", data: null, error: audit.error, reload };
  if (catalog.status === "loading" || audit.status === "loading") {
    return { status: "loading", data: null, error: null, reload };
  }
  return { status: "ready", data: { catalog: catalog.data, audit: audit.data }, error: null, reload };
}

export function ConsolePage() {
  const health = useResource(useCallback(() => fetchHealth(), []));
  const catalog = useResource(useCallback(() => fetchCatalog(), []));
  const audit = useResource(useCallback(() => fetchAudit(), []));

  return (
    <div className="mx-auto grid w-full max-w-7xl gap-6 p-4 lg:p-6">
      <ConsoleHeader health={health} />

      <main className="grid gap-6">
        <AsyncSection resource={overview(catalog, audit)} label="key figures">
          {({ catalog: data, audit: snapshot }) => <KpiTiles tiles={kpiTiles(data, snapshot)} />}
        </AsyncSection>

        <Tabs defaultValue="catalog">
          <TabsList>
            <TabsTrigger value="catalog">Catalogue</TabsTrigger>
            <TabsTrigger value="stock">Stock</TabsTrigger>
            <TabsTrigger value="purchasing">Purchasing</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
          </TabsList>

          <TabsContent value="catalog">
            <AsyncSection
              resource={catalog}
              label="the catalogue"
              isEmpty={(data) => data.items.length === 0}
            >
              {(data) => <CatalogPanel catalog={data} />}
            </AsyncSection>
          </TabsContent>

          <TabsContent value="stock">
            <AsyncSection resource={overview(catalog, audit)} label="stock">
              {({ catalog: data, audit: snapshot }) => (
                <StockPanel catalog={data} audit={snapshot} />
              )}
            </AsyncSection>
          </TabsContent>

          <TabsContent value="purchasing">
            <AsyncSection
              resource={catalog}
              label="purchase orders"
              isEmpty={(data) => data.purchase_orders.length === 0}
            >
              {(data) => <PurchasingPanel catalog={data} />}
            </AsyncSection>
          </TabsContent>

          <TabsContent value="activity">
            <AsyncSection
              resource={audit}
              label="sales and journals"
              isEmpty={(data) => data.sales.length === 0 && data.journals.length === 0}
            >
              {(data) => <ActivityPanel audit={data} />}
            </AsyncSection>
          </TabsContent>
        </Tabs>
      </main>

      <Separator />

      <footer className="text-sm text-muted-foreground">
        <p>
          Read-only demo surface. Values are integer counts of minor units rendered without a
          currency. JSON: <Link to="/api/health">health</Link>,{" "}
          <Link to="/api/catalog">catalogue</Link>, <Link to="/api/audit">audit</Link>.
        </p>
      </footer>
    </div>
  );
}

export function NotFound() {
  return (
    <main className="mx-auto grid w-full max-w-2xl gap-4 p-6">
      <h1 className="text-2xl font-semibold">No such console page</h1>
      <Alert variant="warning">
        <AlertTitle>Unknown path</AlertTitle>
        <AlertDescription>
          <p>The operator console has one route.</p>
          <Link to="/">Go to the console</Link>
        </AlertDescription>
      </Alert>
    </main>
  );
}

const router = createBrowserRouter([
  { path: "/", element: <ConsolePage /> },
  { path: "*", element: <NotFound /> },
]);

export function App() {
  return <RouterProvider router={router} />;
}
