# FAB Italy E-Invoicing

Italian e-invoicing and document exchange for ERPNext.

## Scope

`fab_italy_edi` is the business-domain app for FAB's Italian document exchange
stack. It owns the ERPNext-facing workflows, canonical document model, and the
operator UX around outbound, inbound, and receipt-driven lifecycle management.

Current responsibilities include:

- outbound SDI flows for invoices, credit notes, and autofatture
- inbound supplier-invoice ingestion into ERPNext payables workflows
- canonical document, receipt, and transmission lifecycle tracking
- provider-agnostic transport orchestration with OpenAPI as the first backend
- operator setup, review, retry, and reconciliation tools

Committed specifications live under [`docs/specs`](docs/specs), including:

- [Phase 1 Scope](docs/specs/phase-1.md)
- [Domain Model](docs/specs/domain-model.md)
- [Workflows](docs/specs/workflows.md)

## Branches

- `develop`: integration branch for testing against Frappe/ERPNext `develop`
- `version-16`: stable branch for Frappe/ERPNext 16

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab-italy-edi.git --branch version-16
bench get-app erpnext --branch version-16
bench --site [site] install-app fab_italy_edi
```

`fab_italy_edi` requires `erpnext` in the bench. The Italian localisation fields it
needs on Company, Address and Customer are installed by this app itself, on top of
the Italy regional module that ships with ERPNext.
`fab_openapi` is the reusable backend app for OpenAPI-based SDI transport and
should also be installed when testing that provider path.

## OpenAPI backend

The seeded **OpenAPI SDI Proxy** provider uses the OpenAPI account flow:

- **Account Email**: OpenAPI login email
- **API Key**: OpenAPI API key
- **Access Token**: optional manual bearer override

Use **Sandbox** for `https://test.sdi.openapi.it` and **Production** for
`https://sdi.openapi.it`. When the provider stays on basic auth mode, FAB
requests bearer tokens from the OpenAPI OAuth endpoints automatically before
polling or submission calls.

## Development

```bash
cd apps/fab_italy_edi
pre-commit install
```

Pre-commit is configured for Ruff, ESLint, Prettier, and PyUpgrade.

## License

GNU Affero General Public License v3.0
