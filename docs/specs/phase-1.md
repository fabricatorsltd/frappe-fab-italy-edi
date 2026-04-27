# Phase 1 Scope

## Problem statement

`fab_italy_edi` must provide a production-oriented Italian document exchange layer for ERPNext v16, using `erpnext_italy` as an installation prerequisite where its Italy-localized fields and legacy helpers are still needed, while keeping FAB responsible for the operator workflow, transport orchestration, and new inbound / outbound automation. The first release must support outbound and inbound operational flows for Italian e-invoicing while keeping the architecture extensible for PEPPOL and NSO.

## Scope

### In scope

- Outbound support for:
  - Sales Invoice
  - Sales Invoice Return / Credit Note
- Inbound support for:
  - supplier e-invoice import into Purchase Invoice
- Channels:
  - SDI via proxy/API
  - SDI via PEC
- Core capabilities:
  - FatturaPA XML generation
  - XML validation against FatturaPA rules
  - queued transmission workflow
  - retries and idempotency
  - receipts and reconciliation
  - audit trail and transmission logs
  - credential and provider configuration
  - manual resend / retry / cancel actions
  - basic dashboard and status reporting
  - public procurement references where applicable, including CIG and CUP
  - company-scoped inbound tax mapping from FatturaPA summary buckets
  - exact inbound mixed-tax handling with row-level Purchase Invoice tax binding
  - disabled-by-default Natura IVA provisioning for inbound scenarios
  - foreign-purchase regularization through an autofattura workflow starting from imported Purchase Invoices

### Out of scope

- Full PEPPOL execution flows
- Full NSO execution flows
- long-term archival / conservazione implementation
- replacing every `erpnext_italy` helper in Phase 1

## Design principles

1. FAB owns the workflow and transport orchestration, while `erpnext_italy` may remain a prerequisite for Italy-localized document fields and transitional XML helpers.
2. Channel abstraction so transport providers do not shape the core business workflow.
3. Async-first execution for network transmission and receipt ingestion.
4. Audit-first persistence of payloads, attempts, and receipts.
5. Idempotent retries and resend rules.
6. Private-by-default storage for XML and receipt artifacts.
7. ERPNext-native UX through document links, statuses, and operator tools.

## Technical architecture

### Internal modules

- `fab_italy_edi.core`
- `fab_italy_edi.fatturapa`
- `fab_italy_edi.channels`
- `fab_italy_edi.channels.sdi_proxy`
- `fab_italy_edi.channels.sdi_pec`
- `fab_italy_edi.receipts`
- `fab_italy_edi.imports`
- `fab_italy_edi.dashboard`
- `fab_italy_edi.guidance`

### Provider strategy

- Define a generic provider abstraction first.
- Implement OpenAPI as the first SDI proxy adapter.
- Keep PEPPOL and NSO as later channel families, not SDI special cases.

## Acceptance criteria

Phase 1 is complete when:

1. A Sales Invoice or Credit Note can generate validated FatturaPA XML.
2. A valid document can be queued and sent through either SDI proxy or PEC.
3. Retries cannot create duplicate submissions for the same source document version.
4. Receipts update the linked EDI record and source ERPNext document consistently.
5. Supplier XML can be imported into a native Purchase Invoice review draft with duplicate protection.
6. Operators can inspect attempts and receipts, then trigger retry/resend/status refresh actions.
7. The app provides a basic operational dashboard for failures, pending receipts, and recent activity.
8. Inbound supplier invoices with mixed VAT/Natura buckets preserve exact XML totals and bind the correct tax per item row.
9. A manually imported foreign Purchase Invoice can drive a guided autofattura flow that keeps source and generated documents linked and steers the operator to use a dedicated numbering series.

## Current implementation status

Implemented in the current app:

- outbound send via OpenAPI proxy with callback / polling lifecycle scaffolding
- inbound supplier-invoice ingestion into `EDI Document`
- native Purchase Invoice review drafts instead of a separate inbox doctype flow
- manual supplier selection / creation from staged inbound data, with linked Address, Contact, and Bank Account materialization
- company-scoped inbound tax mappings on `EDI Configuration`
- exact inbound tax import using fixed document tax rows plus row-level `Item Tax Template` binding
- ABI/CAB-backed bank resolution through the companion `fab_banks_import` app
- standard Natura IVA account + mapping provisioning as disabled defaults, with lazy enablement when an inbound invoice uses a mapped Natura bucket
- site installation now requires `erpnext_italy` in addition to ERPNext
- foreign Purchase Invoices can now be prepared for autofattura inside FAB by choosing `TD17` / `TD18` / `TD19`, validating a dedicated naming series, and creating a linked draft `EDI Document`
- the accounting/tax guidance layer has now been specified as a dedicated follow-up module so Purchase Invoice operators can be guided through expense-vs-asset classification, payable-account currency, foreign-purchase handling, and autofattura readiness

## Remaining Phase 1 follow-up work

- move XML rendering / validation fully under FAB's canonical model instead of relying on ERPNext's Italy XML generator
- validate the remaining simulated SDI outcomes beyond the happy-path delivery flow
- decide whether inbound supplier invoices should keep a poll fallback in addition to callback ingestion
- improve re-sync behavior for already-created inbound Purchase Invoice drafts when item-level import logic evolves
- decide what outbound UX should expose for the seeded-but-disabled Natura IVA catalog
- complete the foreign-purchase regularization workflow after the preparation step by generating the final outbound document payload and XML/send path for `autofattura` (TD17 / TD18 / TD19)
- add guided setup and validation for a dedicated autofattura numbering series so these documents do not share the ordinary outbound sequence
- implement the rule-driven Italian accounting/tax guidance module defined in `docs/specs/guidance-module.md`

## Italy-specific document references

The canonical invoice model must explicitly support public procurement and traceability references such as:

- CIG
- CUP
- order / contract / convention references
- transport or supporting document references where required by the target flow

These references are considered part of the functional scope for outbound invoice modeling, even if some of them are only mandatory for specific business cases.

## Initial backlog

1. Define DocTypes and ERPNext custom fields.
2. Build the canonical invoice model.
3. Implement XML rendering and validation.
4. Implement the EDI lifecycle and state machine.
5. Implement the proxy adapter and OpenAPI adapter.
6. Implement PEC transport and receipt processing.
7. Implement receipt normalization and reconciliation.
8. Implement supplier invoice import.
9. Implement operator actions and dashboard.
10. Add tests and hardening.
