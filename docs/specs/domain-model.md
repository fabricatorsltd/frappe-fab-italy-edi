# Domain Model

## Core DocTypes

### EDI Channel

Defines the logical channel family:

- `sdi_proxy`
- `sdi_pec`
- `peppol`
- `nso`

Key fields:

- channel key
- enabled flag
- handler class path
- declared capabilities

### EDI Provider

Represents a concrete provider or transport implementation.

Key fields:

- provider name
- channel type
- adapter key
- auth mode
- API or mailbox endpoints
- polling / webhook configuration
- active flag

### EDI Configuration

Company-scoped configuration for outbound and inbound behavior.

Key fields:

- company
- default outbound channel
- automatic polling settings
- sender identity data
- secrets references
- PEC mailbox settings
- provider-specific settings
- inbound tax mappings keyed by `tax_rate + optional Natura`
- future foreign-purchase regularization defaults, including the preferred autofattura numbering series

#### EDI Inbound Tax Mapping

Child-table rows owned by `EDI Configuration`.

Key fields:

- tax rate
- Natura code (optional for normal VAT rates, required for Natura buckets)
- target ERPNext tax account

Rules:

- one mapping per `(tax_rate, Natura)` pair
- mappings are company-scoped through their parent `EDI Configuration`
- standard Natura mappings may be seeded automatically during install / migrate

### EDI Document

Canonical record for any outbound or inbound business document handled by the app.

Key fields:

- source doctype and source name
- document kind: invoice, credit_note, supplier_invoice_import
- company
- party references
- channel and provider
- canonical document identifier
- transmission state
- validation state
- latest receipt state
- idempotency key
- generated XML file reference
- latest external submission identifier

The canonical invoice payload built from ERPNext documents must also carry Italy-specific references when applicable, including:

- CIG
- CUP
- customer order references
- contract / convention / administrative references

### EDI Transmission Attempt

Append-only record of every outbound send attempt.

Key fields:

- parent EDI Document
- attempt number
- request payload reference
- response payload reference
- transport outcome
- external status code and message
- retryable flag
- timestamps

### EDI Receipt

Append-only record of inbound transport or SDI responses.

Key fields:

- parent EDI Document
- receipt type
- external message identifier
- raw payload reference
- parsed status outcome
- linked source action taken

## ERPNext document integration

### Sales Invoice custom fields

- e-invoicing enabled flag
- EDI Document link
- transmission status
- latest receipt outcome
- manual action status
- procurement / traceability references such as CIG and CUP where needed by the canonical model

### Purchase Invoice custom fields

- imported from EDI flag
- source EDI Document link
- source XML attachment
- procurement / traceability references captured from inbound XML where present

## Supporting ERPNext records used by inbound import

The inbound flow also relies on native ERPNext records outside FAB-specific DocTypes:

- `Account` for company tax ledgers referenced by inbound mappings
- `Item Tax Template` for row-level mixed-tax binding on imported Purchase Invoices
- `Bank` and `Bank Account` for imported payment / IBAN normalization
- a dedicated outbound document numbering / naming series for autofatture generated from foreign purchases

For Natura IVA handling, FAB may provision a standard catalog of disabled tax accounts under the company tax group, then enable a specific account lazily when an inbound invoice actually uses that Natura bucket.

For foreign-purchase regularization, FAB should guide the operator toward a dedicated autofattura sequence instead of mixing these documents into the normal outbound numbering flow.

## State model

### EDI Document states

- `draft`
- `validation_failed`
- `ready`
- `queued`
- `sending`
- `sent`
- `delivered`
- `accepted`
- `rejected`
- `failed`
- `cancelled`
- `imported`

## Normalized receipt outcomes

- `queued`
- `sent`
- `delivered`
- `accepted`
- `rejected`
- `failed`
- `cancelled`
- `unknown_pending`

## Security and storage rules

- XML, receipts, raw provider payloads, and mailbox artifacts must be stored as private files.
- Secrets must live in password fields or site-config-backed mechanisms, never in plain text.
- Configuration must be company-scoped.
- Attempt and receipt history must be append-only.
- Manual review must exist for unmatched or unparsable receipts.
