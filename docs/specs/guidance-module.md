# Italian Accounting and Tax Guidance Module

## Problem statement

ERPNext exposes the fields needed to book Italian purchase invoices, but it does not explain the accounting and tax consequences of the choices the operator is making. In a small company this produces avoidable errors such as:

- using a fixed-asset account for a recurring software subscription
- booking a foreign-currency supplier invoice against a payable account with the wrong currency
- confusing supplier-invoice currency with card-settlement currency
- trying to force Italian VAT onto the source Purchase Invoice when the correct treatment is a separate autofattura workflow
- failing to prepare an autofattura because naming series, supplier tax identity, or tax setup is incomplete

`fab_italy_edi` should add a guidance layer that makes these decisions understandable and operational instead of leaving the operator alone with low-level ERPNext validation errors.

## Product goal

Provide a rule-driven guidance module for Italian SMEs that:

1. explains what the operator is missing in plain language
2. proposes the correct accounting and tax setup for the specific scenario
3. offers safe setup actions where automation is unambiguous
4. keeps the operator in control for ambiguous or legally sensitive choices

This module is guidance-first, not silent automation-first.

## Scope

### Phase A: Purchase Invoice guidance

Focus first on Purchase Invoice scenarios because they are the main source of operator confusion and the entry point for foreign-purchase autofattura.

Initial coverage:

- manually entered Purchase Invoices
- Purchase Invoices imported from inbound XML
- foreign supplier invoices that require later autofattura handling
- currency / payable-account guidance
- expense-account vs fixed-asset guidance
- row-level tax-treatment guidance
- autofattura readiness checks

### Phase B: Setup assistants

Add guided setup flows for the prerequisites surfaced by the guidance engine, especially:

- payable accounts in matching currency
- recurring-expense accounts under the correct P&L parent
- supplier defaults
- autofattura naming series
- EDI Configuration readiness

### Out of scope for the first guidance release

- replacing a tax consultant or accountant
- fully automatic fiscal classification for legally ambiguous cases
- OCR or AI extraction from PDFs
- automatic posting of accounting entries without explicit operator review

## Design principles

1. **Explain before blocking.** Validation errors must be translated into accounting meaning, not only technical field names.
2. **Use document context.** Recommendations must depend on supplier country, invoice currency, company country, item type, recurrence, and transport/tax workflow.
3. **Separate concerns clearly.** The UI must distinguish:
   - expense / asset classification
   - payable account selection
   - invoice currency
   - payment-settlement mechanics
   - Italian VAT / autofattura treatment
4. **Safe automation only.** The module may create setup artifacts only where the outcome is deterministic and reversible.
5. **Traceable guidance.** The operator must be able to see why a recommendation was made.

## Operator questions the module must answer

For each Purchase Invoice, the guidance module should answer:

1. **What did I buy?**
   - recurring expense
   - one-off operating cost
   - capitalizable asset / intangible

2. **In which currency should I book the invoice?**
   - source supplier invoice currency
   - not the card-settlement or bank-settlement currency

3. **Which payable account should I use?**
   - account currency must match document currency
   - supplier defaults should be proposed when available

4. **What is the Italian tax treatment?**
   - domestic VAT
   - Natura / exempt / excluded
   - foreign purchase requiring autofattura / reverse-charge workflow

5. **Can I prepare the autofattura yet?**
   - supplier identity complete enough
   - foreignness resolved
   - naming series configured
   - required account/tax setup complete

## Primary UX surfaces

### 1. Purchase Invoice guidance banner

Show a summary banner at the top of the form with severity:

- **green**: document looks ready
- **orange**: document can be saved but needs operator review
- **red**: blocking setup issue

Example messages:

- "Invoice currency is USD but payable account 2110 - Creditors - fab is in EUR. Use a USD creditors account or book the invoice in EUR."
- "Jira looks like a recurring software subscription. Use an expense account, not a fixed-asset account."
- "This foreign supplier purchase appears to require autofattura. Configure naming series and prepare TD17 after reviewing supplier tax identity."

### 2. Guided detail panel

Add a structured panel or dialog with:

- findings
- explanation
- recommended values
- quick actions

Each finding should expose:

- severity (`info`, `warning`, `blocking`)
- title
- explanation
- affected fields
- suggested action

### 3. Setup actions

The module should expose targeted actions such as:

- **Create USD Creditors Account**
- **Create Software Subscription Expense Account**
- **Set Supplier Default Payable Account**
- **Open EDI Configuration**
- **Configure Autofattura Naming Series**
- **Prepare Autofattura**

## Decision inputs

The guidance engine should consider at least:

- company country and base currency
- supplier country and tax identity
- document currency and conversion rate
- payable account currency
- payment method hints (credit card, bank transfer, direct debit)
- item names, item groups, UOM, recurrence hints, and existing item master data
- expense account type and parent account
- item tax template and document tax rows
- whether the source document was imported from XML / EDI
- autofattura configuration status on `EDI Configuration`

## Guidance rules

### A. Expense vs asset rules

- Recurring software subscriptions, SaaS, hosting, cloud services, and monthly licenses should default to **operating expense**, not fixed assets.
- Capitalization should be suggested only when the operator explicitly classifies the purchase as a long-term asset / intangible asset.
- If the operator selects an account under `Fixed Assets` for a clearly recurring subscription, show a warning and recommend an expense account.

### B. Payable-account currency rules

- If `Purchase Invoice.currency` differs from the selected payable account currency, show a blocking finding.
- Guidance must explain that card settlement or Amex settlement currency does not change the supplier invoice currency.
- If a matching payable account does not exist, offer a setup action to create one.

### C. Foreign purchase tax rules

- If company country is Italy and supplier is foreign, guidance must evaluate whether the document should later drive an autofattura / reverse-charge workflow.
- Guidance must explain that the source Purchase Invoice may legitimately carry no Italian VAT rows when VAT is regularized via autofattura.
- The operator should be guided toward `TD17`, `TD18`, or `TD19` based on explicit workflow rules and confirmation, not hidden automation.

### D. Supplier-identity readiness rules

- Warn when supplier country is missing or inconsistent with tax identity.
- Warn when supplier tax/VAT identity is missing for a document that will later need autofattura XML.
- Offer quick navigation to fill the missing supplier master data.

### E. Imported XML rules

- For inbound XML, explain whether taxes were mapped from the XML summary, whether Natura mappings were applied, and whether any buckets remain unresolved.
- Keep row-level tax findings tied to the specific item rows when possible.

## Setup assistant requirements

### 1. Payable-account setup assistant

When a matching payable account is missing, the assistant should:

1. propose the correct parent account under liabilities / payables
2. propose account currency based on document currency
3. optionally mark it as supplier default
4. return the operator to the Purchase Invoice with the field populated

### 2. Expense-account setup assistant

When the selected cost account is clearly wrong or missing, the assistant should:

1. propose a P&L parent account
2. propose a semantic account name such as `Software Subscriptions - fab`
3. populate the row after creation

### 3. Autofattura sequence assistant

The module should guide the operator through naming-series readiness:

1. explain why autofattura needs a dedicated sequence
2. let the operator create or select the series
3. persist it in `EDI Configuration.autofattura_naming_series`
4. return to the source Purchase Invoice and refresh guidance state

## Technical shape

### Initial implementation strategy

Start with a deterministic rule engine, not an LLM-driven assistant.

Suggested module layout:

- `fab_italy_edi.guidance`
- `fab_italy_edi.guidance.purchase_invoice`
- `fab_italy_edi.guidance.rules`
- `fab_italy_edi.guidance.actions`

Suggested response shape:

```python
{
  "status": "warning",
  "findings": [
    {
      "code": "payable_currency_mismatch",
      "severity": "blocking",
      "title": "Payable account currency does not match document currency",
      "explanation": "...",
      "fields": ["currency", "credit_to"],
      "actions": ["create_payable_account", "change_document_currency"]
    }
  ],
  "recommended_actions": [...]
}
```

### Persistence

For the first release, findings may be computed on demand and do not require a dedicated persistent doctype.

Optional future persistence:

- dismissed findings per document
- operator acknowledgment logs
- company-specific rule overrides

## Example scenario: Atlassian / Jira in USD, paid by Amex in EUR

Input:

- supplier: Atlassian Pty Ltd
- company: Italian company
- invoice currency: USD
- settlement method: Amex card in EUR
- line: Jira monthly subscription

Expected guidance:

1. classify the cost as **operating expense**, not fixed asset
2. explain that document currency should follow the supplier invoice, not Amex settlement
3. require a **USD creditors** account if the Purchase Invoice stays in USD
4. explain that Amex FX spread / fees are separate from the supplier invoice
5. mark the document as a likely **foreign service** purchase and surface autofattura readiness guidance
6. block autofattura preparation until the dedicated naming series is configured

## Acceptance criteria

The guidance module is acceptable when:

1. A new Purchase Invoice with a foreign supplier can show targeted guidance before submit-time validation errors become opaque.
2. Choosing a fixed-asset account for a recurring SaaS line triggers an explicit warning with a recommended expense account.
3. Choosing a payable account with currency different from the document currency triggers a blocking finding with a corrective action.
4. A foreign Purchase Invoice can clearly show whether it is ready for autofattura and what is missing if not.
5. Operators can launch setup actions from the guidance UI instead of manually hunting through ERPNext setup screens.
6. Inbound imported invoices keep their XML-derived tax findings visible in the same guidance surface.

## Open decisions

- Whether guidance findings should live only in the form UI or also persist for audit/review
- Whether the setup assistants should create accounts directly or prefill normal ERPNext account forms
- How aggressively the module should infer recurrence and account classification from item names vs explicit item/category metadata
- Whether the first release should cover Sales Invoice guidance too, or stay Purchase-Invoice-first
