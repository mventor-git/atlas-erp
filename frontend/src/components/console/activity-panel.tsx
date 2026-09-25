import { Badge } from "@/components/ui";
import { money } from "@/lib/format";
import type { Audit } from "@/lib/schemas";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";

/** The five newest entries, newest last, matching the server console's window. */
const RECENT = 5;

export function ActivityPanel({ audit }: { audit: Audit }) {
  const name = new Map(audit.items.map((item) => [item.item_id, item.name || item.sku]));
  const sales = audit.sales.slice(-RECENT);
  const journals = audit.journals.slice(-RECENT);

  return (
    <section aria-labelledby="activity-heading" className="grid gap-6">
      <div>
        <h2 id="activity-heading" className="text-lg font-semibold">
          Recent sales and journals
        </h2>
        <p className="text-sm text-muted-foreground">
          Process-local state from <code>/api/audit</code>, newest last. Every sale posts a cash
          debit and a revenue credit for the same total.
        </p>
      </div>

      <div className="grid gap-3">
        <h3 className="text-base font-semibold">Manual sales</h3>
        <Table>
          <TableCaption className="sr-only">Posted manual cash sales.</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Sale</TableHead>
              <TableHead scope="col">Customer</TableHead>
              <TableHead scope="col">Lines</TableHead>
              <TableHead scope="col" className="text-right">
                Total
              </TableHead>
              <TableHead scope="col">Journal</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sales.map((sale) => (
              <TableRow key={sale.sale_id}>
                <TableCell className="font-medium">{sale.sale_id}</TableCell>
                <TableCell>{sale.customer_id}</TableCell>
                <TableCell className="text-muted-foreground">
                  {sale.lines
                    .map((line) => `${name.get(line.item_id) ?? line.item_id} x${line.quantity}`)
                    .join(", ")}
                </TableCell>
                <TableCell className="text-right tabular-nums">{money(sale.total_cents)}</TableCell>
                <TableCell>{sale.journal_id}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <div className="grid gap-3">
        <h3 className="text-base font-semibold">General ledger</h3>
        <Table>
          <TableCaption className="sr-only">Posted journal entries and their balance check.</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Entry</TableHead>
              <TableHead scope="col">Sale</TableHead>
              <TableHead scope="col" className="text-right">
                Debits
              </TableHead>
              <TableHead scope="col" className="text-right">
                Credits
              </TableHead>
              <TableHead scope="col">Check</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {journals.map((journal) => {
              const balanced = journal.total_debits_cents === journal.total_credits_cents;
              return (
                <TableRow key={journal.journal_id}>
                  <TableCell className="font-medium">{journal.journal_id}</TableCell>
                  <TableCell>{journal.sale_id ?? "-"}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {money(journal.total_debits_cents)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {money(journal.total_credits_cents)}
                  </TableCell>
                  <TableCell>
                    <Badge variant={balanced ? "success" : "danger"}>
                      {balanced ? "balanced" : "unbalanced"}
                    </Badge>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
