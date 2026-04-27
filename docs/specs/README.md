# Specifications

This folder contains the committed product and engineering specifications for `fab_italy_edi`.

## Current set

- [Phase 1 Scope](phase-1.md)
- [Domain Model](domain-model.md)
- [Workflows](workflows.md)
- [Italian Accounting and Tax Guidance Module](guidance-module.md)

## Current implementation focus captured by the specs

The committed specs now reflect the current inbound-delivery shape already implemented in the app, including:

- native Purchase Invoice review drafts for inbound supplier invoices
- company-scoped inbound tax mapping (`rate + Natura -> account`)
- exact mixed-tax import with row-level binding via `Item Tax Template`
- disabled-by-default Natura IVA account provisioning with inbound auto-enable
- Purchase Invoice autofattura preparation with TD17 / TD18 / TD19 choice and dedicated sequence validation
- a committed product spec for the future Italian accounting/tax guidance layer on Purchase Invoice and autofattura setup

The same spec files also track the remaining follow-up work so product and engineering notes do not drift apart, including the planned autofattura workflow for manually imported foreign purchases.

## Scope baseline

The current baseline is Phase 1:

- SDI via proxy/API
- SDI via PEC
- provider abstraction with OpenAPI as the first concrete adapter
- future-ready structure for PEPPOL and NSO

The specs are intended to evolve with implementation and should be updated alongside major design changes.
