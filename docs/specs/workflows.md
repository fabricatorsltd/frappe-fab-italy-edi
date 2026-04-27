# Workflows

## Outbound invoice workflow

1. User submits a Sales Invoice or Sales Invoice Return.
2. The app validates legal identity, routing data, tax completeness, payment data, and document eligibility.
3. The app creates or updates an `EDI Document`.
4. The app builds the canonical invoice representation.
5. The app renders FatturaPA XML.
6. The app validates the XML before queuing.
7. A background job sends the document through the selected channel/provider.
8. The app stores the transmission attempt and provider response.
9. The `EDI Document` status is updated.
10. Later receipts reconcile back to the EDI record and source ERPNext document.

## SDI proxy workflow

The proxy channel must support:

- secure credential loading
- request payload construction
- outbound send
- status refresh
- webhook parsing where supported
- receipt normalization

The first concrete implementation target is OpenAPI, behind a generic provider contract.

## PEC workflow

### Outbound

1. Resolve company-level PEC configuration.
2. Generate and validate the XML payload.
3. Package and send through the configured PEC mailbox.
4. Persist transport-level message identifiers.

### Inbound

1. Poll the configured PEC mailbox.
2. Persist raw mailbox artifacts privately.
3. Parse known SDI-related receipt messages.
4. Reconcile receipts to an `EDI Document`.
5. Route unmatched messages to manual review.

## Receipt and reconciliation workflow

Receipts from proxy or PEC must normalize to the same internal outcome model.

Rules:

- only one current state per `EDI Document`
- append-only attempt and receipt history
- explicit status transition rules
- manual review path for unmatched or invalid receipts

## Purchase invoice import workflow

Inbound supplier XML must be supported from:

- proxy/provider retrieval
- PEC retrieval
- manual operator import fallback

Flow:

1. Load XML and compute duplicate protection keys.
2. Parse supplier, line, payment, and tax previews into the staging `EDI Document`.
3. Match existing Suppliers and let the operator choose the target Supplier; supplier creation remains an explicit operator action, not an automatic side effect of import.
4. Create or refresh a native Purchase Invoice review draft linked back to the `EDI Document`.
5. Build Purchase Invoice lines from XML line details, including business descriptions derived from fields such as `CodiceTipo` / `RiferimentoTesto` when present.
6. Apply company-scoped inbound tax mappings from FatturaPA summary buckets.
7. Preserve exact XML tax totals on the document and persist row-level tax applicability through `Item Tax Template` and item-wise tax details.
8. For mapped Natura buckets, enable the corresponding disabled-by-default Natura IVA account automatically when that inbound invoice uses it.
9. Store the source XML privately.
10. Link the created or refreshed Purchase Invoice back to the `EDI Document`.

## Purchase Invoice guidance workflow

1. The operator opens or edits a Purchase Invoice.
2. FAB evaluates document context such as supplier country, invoice currency, payable-account currency, selected expense account, item tax treatment, and autofattura readiness.
3. FAB shows a guidance summary with explicit severity (`green`, `orange`, `red`) instead of waiting for opaque ERPNext validation popups.
4. The operator can inspect detailed findings, each with explanation, affected fields, and recommended actions.
5. Safe setup actions such as creating a matching-currency payable account, creating a recurring-expense account, or opening autofattura configuration can be launched directly from the guidance UI.
6. Guidance refreshes after each relevant field change so the operator can see when the document is actually ready.

### Inbound tax setup workflow

1. On install / migrate, seed the standard Natura IVA catalog as disabled tax accounts under each company tax group.
2. Backfill `EDI Configuration` records with default inbound mappings for standard `0% + Natura` buckets without overwriting company-specific custom mappings.
3. Keep these seeded tax accounts disabled by default so the chart of accounts does not become noisy for outbound operators.
4. When an inbound invoice uses a mapped Natura bucket, enable that specific account automatically so the imported Purchase Invoice can post correctly.
5. Outbound use of those accounts remains an explicit operator choice.

### Foreign purchase autofattura workflow

1. The operator starts from a manually imported foreign `Purchase Invoice`.
2. FAB exposes a dedicated action such as **Create Autofattura** only when the source invoice qualifies for foreign-purchase regularization.
3. A guided wizard lets the operator choose the required document type (`TD17`, `TD18`, or `TD19`) and confirm the fiscal dates and tax treatment.
4. The wizard must guide the operator to use a dedicated autofattura numbering series; if no dedicated series is configured, FAB should block the workflow with clear setup instructions instead of silently reusing the standard outbound sequence.
5. FAB creates the outbound regularization document inside the existing app workflow, keeping an explicit link back to the source foreign Purchase Invoice.
6. The generated autofattura then enters the normal outbound XML generation, validation, and transmission lifecycle already used by FAB.
7. Source purchase and generated autofattura must remain traceable from each side for audit and operator review.

## Manual operator actions

Operators must be able to:

- retry failed transmission
- resend a corrected document as a new submission
- cancel queued sends before dispatch
- trigger manual status refresh
- inspect attempts, receipts, and raw payloads

## Dashboard and reporting

Phase 1 reporting must expose:

- documents by status
- failed transmissions needing action
- unmatched receipts
- recent sends by provider and channel
- imported supplier invoices
