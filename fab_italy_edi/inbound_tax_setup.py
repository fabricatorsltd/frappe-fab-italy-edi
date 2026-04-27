from __future__ import annotations

from typing import Any, Mapping

import frappe
from frappe.utils import cint, flt

from erpnext.setup.setup_wizard.operations.taxes_setup import get_or_create_account


STANDARD_INBOUND_NATURA_TAX_TYPES: tuple[dict[str, str], ...] = (
	{"nature": "N1", "account_name": "VAT Natura N1 - Excluded ex art. 15"},
	{"nature": "N2", "account_name": "VAT Natura N2 - Not subject (legacy)"},
	{"nature": "N2.1", "account_name": "VAT Natura N2.1 - Not subject arts. 7-7 septies"},
	{"nature": "N2.2", "account_name": "VAT Natura N2.2 - Not subject other cases"},
	{"nature": "N3", "account_name": "VAT Natura N3 - Non taxable (legacy)"},
	{"nature": "N3.1", "account_name": "VAT Natura N3.1 - Export"},
	{"nature": "N3.2", "account_name": "VAT Natura N3.2 - Intra-EU supply"},
	{"nature": "N3.3", "account_name": "VAT Natura N3.3 - San Marino"},
	{"nature": "N3.4", "account_name": "VAT Natura N3.4 - Related operations"},
	{"nature": "N3.5", "account_name": "VAT Natura N3.5 - Declaration of intent"},
	{"nature": "N3.6", "account_name": "VAT Natura N3.6 - Other non taxable"},
	{"nature": "N4", "account_name": "VAT Natura N4 - Exempt"},
	{"nature": "N5", "account_name": "VAT Natura N5 - Margin scheme"},
	{"nature": "N6", "account_name": "VAT Natura N6 - Reverse charge (legacy)"},
	{"nature": "N6.1", "account_name": "VAT Natura N6.1 - Reverse charge scrap"},
	{"nature": "N6.2", "account_name": "VAT Natura N6.2 - Reverse charge gold and silver"},
	{"nature": "N6.3", "account_name": "VAT Natura N6.3 - Reverse charge subcontracting"},
	{"nature": "N6.4", "account_name": "VAT Natura N6.4 - Reverse charge buildings"},
	{"nature": "N6.5", "account_name": "VAT Natura N6.5 - Reverse charge mobile phones"},
	{"nature": "N6.6", "account_name": "VAT Natura N6.6 - Reverse charge electronics"},
	{"nature": "N6.7", "account_name": "VAT Natura N6.7 - Reverse charge energy"},
	{"nature": "N6.8", "account_name": "VAT Natura N6.8 - Reverse charge public sector"},
	{"nature": "N6.9", "account_name": "VAT Natura N6.9 - Reverse charge other cases"},
	{"nature": "N7", "account_name": "VAT Natura N7 - VAT paid in other EU state"},
)


def get_standard_inbound_natura_tax_types() -> list[dict[str, str]]:
	return [dict(row) for row in STANDARD_INBOUND_NATURA_TAX_TYPES]


def ensure_standard_inbound_natura_setup() -> None:
	for row in frappe.get_all("Company", fields=["name"]):
		ensure_standard_inbound_natura_accounts(row["name"])

	for row in frappe.get_all("EDI Configuration", fields=["name"]):
		configuration = frappe.get_doc("EDI Configuration", row["name"])
		if ensure_standard_inbound_natura_configuration(configuration):
			configuration.save(ignore_permissions=True)


def ensure_standard_inbound_natura_configuration(configuration) -> bool:
	company = getattr(configuration, "company", None)
	if not company:
		return False

	account_heads = ensure_standard_inbound_natura_accounts(company)
	return append_missing_standard_inbound_natura_mappings(configuration, account_heads=account_heads)


def ensure_standard_inbound_natura_accounts(company: str) -> dict[str, str]:
	account_heads = {}
	for row in STANDARD_INBOUND_NATURA_TAX_TYPES:
		account = get_or_create_account(
			company,
			{
				"account_name": row["account_name"],
				"disabled": 1,
			},
		)
		account_heads[row["nature"]] = account.name
	return account_heads


def append_missing_standard_inbound_natura_mappings(
	configuration,
	*,
	account_heads: Mapping[str, str],
) -> bool:
	existing_keys = {
		build_mapping_key(row.get("tax_rate"), row.get("nature"))
		for row in (configuration.get("inbound_tax_mappings") or [])
	}
	changed = False
	for row in STANDARD_INBOUND_NATURA_TAX_TYPES:
		key = build_mapping_key(0.0, row["nature"])
		if key in existing_keys:
			continue
		configuration.append(
			"inbound_tax_mappings",
			{
				"tax_rate": 0.0,
				"nature": row["nature"],
				"account_head": account_heads[row["nature"]],
			},
		)
		existing_keys.add(key)
		changed = True
	return changed


def ensure_inbound_natura_account_enabled(account_head: str | None, *, nature: Any = None) -> str | None:
	account_head = normalize_text(account_head)
	if not account_head or not is_standard_inbound_natura(nature):
		return account_head

	if cint(frappe.db.get_value("Account", account_head, "disabled")):
		frappe.db.set_value("Account", account_head, "disabled", 0, update_modified=False)
	return account_head


def is_standard_inbound_natura(nature: Any) -> bool:
	return normalize_nature(nature) in {row["nature"] for row in STANDARD_INBOUND_NATURA_TAX_TYPES}


def build_mapping_key(tax_rate: Any, nature: Any) -> tuple[float, str]:
	return round(flt(tax_rate), 6), normalize_nature(nature)


def normalize_nature(value: Any) -> str:
	return (normalize_text(value) or "").upper()


def normalize_text(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None
