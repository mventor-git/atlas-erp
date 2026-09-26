# Legacy Knowledge Transfer

## Purpose and rule

The legacy `ecom-erp` project is the **evidence base** for Atlas ERP requirements,
not its authority. It is a read-only reference. This document records *what* the
legacy project proved and *what* is explicitly not carried, so that a future
reader can tell an inherited requirement from an inherited accident.

The rule, restating `contract.md` v1.3.0 § Legacy knowledge transfer:

- **Adopt** the invariants, requirements, and tests a legacy area proved. Each is
  restated here as a requirement of this product and, where it is a rule, must
  exist as a test that runs in this repository.
- **Redesign** the mechanism behind every adopted requirement inside
  `core -> cluster -> plugin -> module`, against this product's own
  `atlas_erp` database and operator console.
- **Drop** the legacy accidents. They are known defects, not a starting point.
- **No copy.** No legacy source, schema, route, or UI is carried over, and no
  legacy table is read, imported, or migrated. Legacy data is still not imported
  (unchanged from the v1.0.0 non-goal).
- Where a legacy behavior is ambiguous, **this contract decides** and the legacy
  repository is cited as evidence for the requirement only.

Citations below are `path:line` in `C:\Users\Mventor\github\repositories\ecom-erp`
at `0fd29bc`. They point at evidence. Nothing here is copied from them.

## Adopted ERP requirements

Each row is a requirement this product inherits as a *rule*. The mechanism column
says nothing is inherited with it.

| # | Requirement | Legacy evidence | Mechanism here |
| --- | --- | --- | --- |
| R1 | **Money is integer minor units, always.** A fractional, missing, or non-numeric amount is refused loudly, never rounded and never silently truncated. | `server/services/journalService.js:29-39` (the integer-cents gate), `server/db.js:367` (`Money INTEGER cents (ADR-014)`), `server/routes/adminSale.js:42`, `server/services/apAgingService.js:141`, `server/tests/accountingHardening.test.js:140-165` | `finance` |
| R2 | **Double entry, or nothing.** A journal entry needs >=2 effective lines, no negative amounts, never debit and credit both positive on one line, total debit == total credit and > 0, every line an existing active account, and an immutable assigned number. Writes are transactional. | `server/services/journalService.js:8-14` (the stated invariants), `server/db.js:384-385` | `finance` |
| R3 | **Corrections are reversals, never edits or unposts.** A posted entry is immutable; a correction posts a dated opposite entry that stays visible beside the original, exactly once, with a required reason. Sourced entries are reversed by their own source service, not by unposting. | `server/services/journalService.js:291-310` (unpost refused), `:325-353` (reversal, one per entry, reason required), `server/db.js:417-418`, `server/services/supplierPaymentService.js:39`, `server/routes/admin.js:1095-1114` | `finance` |
| R4 | **Periods fail closed.** While a financial period is closed, posting and stock movement are refused; reopening is a distinct, audited action; recovery is proven, not assumed. | `server/services/inventoryService.js:75`, `server/services/journalService.js:306-310`, `server/services/warehouseOrderService.js:643-646`, `server/routes/warehouseOrders.js:178`, `server/services/eventService.js:90-91`, `server/tests/glOperability.test.js:176,429`, `server/tests/periodReopen.test.js`, `server/tests/inventoryGL093.test.js:190` | `finance`, `inventory` |
| R5 | **Stock is movement-derived.** Every quantity change is a typed movement row; the movement ledger is the source of truth and any register is a derived cache rebuildable by replay. | `server/db.js:804` (`Inventory Movements - the source of truth for stock`), `server/services/inventoryService.js:4-30`, `server/services/productTrashService.js:7-11` (rebuild by replaying movement sums), `server/tests/financialPeriod.test.js:4,72-84` | `inventory` |
| R6 | **One posting per source event.** Replaying a source returns the existing posted entry rather than a second one, an interrupted attempt is recoverable, and a movement that cannot be valued is *listed*, never fabricated. | `server/services/inventoryPosting.js:23-32` (one journal per movement), `server/services/idempotencyService.js:40-58` (canonical request fingerprint), `server/tests/idempotencyN1.test.js`, `server/tests/checkoutIdempotencyN2.test.js`, `server/tests/inventoryGL093.test.js:190` | `finance`, `inventory` |
| R7 | **Reports are derived from posted truth, and they self-report.** Statements and aging read posted journals rather than a stored total, and a reconciliation report publishes its own control totals plus a boolean saying whether they tie. | `server/services/ledgerService.js:140` (P&L strictly journal-derived), `server/services/apAgingService.js:4` and `:281-298`, `server/routes/accounting.js:179-192`, `server/routes/admin.js:1237-1247`, `server/tests/apAging.test.js:230-232,486-499` | `finance` |
| R8 | **Import is parse -> preview -> commit, all-or-nothing.** Business semantics live in one place, never in a route or a component; duplicates are a validation error for the operator to fix, not a silent first-or-last-wins; an update never writes stock directly, it posts a real movement. | `server/services/onboardingImport.js:1-17`, `server/routes/productsImportExport.js:1-5`, `server/tests/onboardingImport095.test.js:170` | `purchasing`, `masterdata` |
| R9 | **One document sequence per document kind, and an unconfigured kind is refused.** A sequence is a configured prefix/separator/year/padding shape; an unknown or unconfigured document type fails rather than inventing a number. | `server/services/documentNumberService.js:3,29-32`, `server/routes/documentNumbers.js`, and the real defect it caused at `docs/archive/legacy/production-hardening-changelog.md:368` (`No sequence configured for document type: PO`) | `purchasing` |

R7's spirit also rules out a legacy pattern recorded in
`server/services/salesSettlementService.js:99`: record the money and never fake a
status. Status-only settlement is an accident, not a requirement.

## Candidate evolution map

Mirrors `contract.md` v1.3.0 § Evolution map. **A candidate is not approved
scope.** Nothing below ships until an amendment promotes it, and a promoted
module still has to pass the transfer gate at the end of this document.

| Cluster | Candidate modules | Requirements above |
| --- | --- | --- |
| `finance` | `journal` — balanced entries, posted-only truth | R1, R2, R3, R7 |
| `finance` | `period` — open and close, fail-closed while closed | R4 |
| `finance` | `reconciliation` — operational state against posted journals | R7 |
| `inventory` | `movement` — ledger of every quantity change | R5 |
| `inventory` | `valuation` — cost captured per movement, never restated | R5, R6 |
| `inventory` | `replay` — rebuild derived state from movements | R5 |
| `purchasing` | `numbering` — document sequences per document kind | R9 |
| `purchasing` | `import` — external purchase data into purchasing | R8 |

**Explicitly candidate decisions, not active scope:** pricing (price lists,
markup, cost policy) and operations (picking, packing, delivery). The legacy
project has material for both, and the interesting parts are contested — see
`docs/archive/legacy/current-state-matrix.md:18` and `:24` in `ecom-erp`, where
FIFO cost layers exist but standard sales ignore them and COGS is computed from
the *current* product cost. That is a duplicate valuation truth, so the cost
policy is a decision this product has not made yet, not a requirement to copy.
Promoting either area is a contract amendment.

## Anti-patterns explicitly not carried

These are the legacy project's own recorded defects. Each is named here so a
later reader does not reintroduce it while "restoring parity".

| Anti-pattern | Legacy evidence | Why it is not carried |
| --- | --- | --- |
| `sql.js` as the store | `server/db.js:1,22`; `docs/archive/legacy/verdict-report.md:4`; single-writer/in-process at `docs/archive/legacy/production-hardening-changelog.md:122` | An in-process single-writer engine cannot be the contract's PostgreSQL `atlas_erp` database, and it makes real concurrency untestable (`server/tests/concurrency.test.js:3`). |
| A God `db.js` | `server/db.js` (schema, migrations, seeds, and helpers in one file; `docs/archive/legacy/verdict-report.md:27,178`) | Collapses the `core -> cluster -> plugin -> module` boundary into one module and makes every table a shared dependency. |
| Implicit inline migrations | `server/db.js:34` onward (a long run of `try { db.run("ALTER TABLE ... ADD COLUMN ...") } catch {}`), plus `server/db.js:564-568` noting `migrations/004`+`005` are defined but nothing executes them, and four unreferenced files `server/migrations/004..007_*.sql`; `docs/archive/legacy/verdict-report.md:75`, `docs/archive/legacy/current-state-matrix.md:25` | No versioned migration table, no history, no down-migration, and a `catch {}` that swallows a real failure. Schema change is a deliberate, reviewable operation. |
| Duplicate stock truth | Legacy `products.stock` kept "for compat" beside the movement ledger and the `inventory` register, at `docs/archive/legacy/current-state-matrix.md:24` and `server/services/productTrashService.js:7-11` | One truth per fact. A counter that is a second source of stock cannot be reconciled against anything. |
| Unnormalized order payloads | `orders.items` as a JSON blob, `docs/archive/legacy/current-state-matrix.md:24` | Line-level truth has to be queryable, joinable, and reconcilable; a blob makes R7 unreportable. |
| Current-cost COGS | FIFO layers dormant while sales read current `products.cost_price`, `docs/archive/legacy/current-state-matrix.md:18`, `docs/archive/legacy/verdict-report.md:45` | Restating history on a price change is a valuation defect; cost is captured per movement and never restated. |
| Provider-specific code in the domain | One named payment provider wired directly through `server/routes/kashier.js`, `kashierCheckout.js`, `kashierWebhook.js`, `services/kashierService.js`, and `services/kashierWebhookService.js` (a *domain* service named for a provider); no abstraction, and a mock adapter marked blocked and unwired — `docs/archive/legacy/current-state-matrix.md:16`, `server/services/mockPaymentProvider.js:3` | Payments are outside this product's boundary per the v1.0.0 non-goals, and a provider name in a core service is a coupling the contract does not accept. |
| Fake or placeholder UI | `docs/archive/legacy/verdict-report.md:110-113` and `docs/archive/legacy/ui-ux-verdict-report.md:149` (Reviews and Wishlist tabs are literal placeholder text); `VipInvitations.jsx` local-state-only at `docs/system-audit-2026-08-28.md:165`; the "placeholder-as-implemented" hunt at `docs/archive/notes/28-08-2026-todolist.md:90,98` | A screen that looks like a capability and is not is worse than an absent one. This is why the transfer gate below requires defined loading, empty, error, and success states. |
| The `isAdmin` backdoor | `server/middleware/rbac.js:18,86` and `server/routes/onboarding.js:71` grant `permissions: ['*']` purely on `isAdmin && !userId`; `server/routes/admin.js:191` mints a `super_admin` session for an environment admin | Authorization must derive from a real identity and a real permission, and the tests show how easily the backdoor is relied on (`server/tests/purchaseOrder.test.js:20-21`). |
| Status-only settlement | `server/services/salesSettlementService.js:99` | A status change is not money. Money is posted or it did not happen. |

## Acceptance-gate template

This is the per-capability form of `contract.md` v1.3.0 acceptance gate 7. A
capability whose requirement was adopted above ships only with every field
below filled in. An absent, skipped, or stubbed test is not evidence for the
gate.

```markdown
### <capability name>

- Owning cluster:      <one of the baseline clusters>
- Requirement adopted: <R1..R9, and the legacy path that proved it>
- Public seam:         <the callable surface other modules are allowed to use>
- Invariant:           <the one rule that must always hold, in one sentence>
- Failure semantics:   <what is refused, with what error, and what is left unchanged>
- Test:                <path to the test that runs, and what it asserts>
- UI states:           <loading, empty, error, success — all four defined>
```

Two rules apply to every filled block:

- The invariant must be checkable without reading the implementation, and the
  test must fail if the invariant breaks.
- No field may name a legacy module, table, route, or screen as the mechanism.
  If a field can only be filled with a legacy reference, the design has not been
  redone yet.
