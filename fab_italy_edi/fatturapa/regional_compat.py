"""FatturaPA helpers that ERPNext no longer exposes.

``erpnext.regional.italy.utils`` used to provide ``get_address_country_code``,
``prepare_e_invoice_address`` and ``get_attachment_progressive_name_and_number``.
They are gone from the module shipped with ERPNext v16, so they are reimplemented
here against the Address custom fields this app installs.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.regional.italy.utils import get_progressive_name_and_number


def get_address_country_code(address) -> str | None:
	"""Return the ISO 3166-1 alpha-2 code for an Address.

	Prefers the ``country_code`` custom field and falls back to the code stored
	on the linked Country, so an Address saved before the field existed still
	resolves.
	"""
	if address is None:
		return None

	country_code = cstr(address.get("country_code") or "").strip()
	if not country_code:
		country = cstr(address.get("country") or "").strip()
		if country:
			country_code = cstr(frappe.get_cached_value("Country", country, "code") or "").strip()

	return country_code.upper() or None


def prepare_e_invoice_address(address):
	"""Normalise an Address document for FatturaPA rendering.

	Enforces the fields ``build_xml_address`` does not check itself, and backfills
	``country_code`` from the linked Country. Missing country codes are left as
	None rather than raising, because foreign suppliers may carry the country in
	the tax id instead.
	"""
	for fieldname in ("address_line1", "city"):
		if not cstr(address.get(fieldname) or "").strip():
			frappe.throw(
				_("Please set {0} for address {1}").format(_(frappe.unscrub(fieldname)), address.name),
				title=_("E-Invoicing Information Missing"),
			)

	address.country_code = get_address_country_code(address)

	return address


def get_attachment_progressive_name_and_number(file_name: str, company_tax_id: str) -> tuple[str, str]:
	"""Recover the progressive name and number from an existing e-invoice file.

	FatturaPA attachments are named ``<company_tax_id>_<progressive>.xml``. When
	the name does not follow that convention a fresh progressive is allocated, so
	a renamed attachment cannot deadlock the transmission.
	"""
	progressive_name = cstr(file_name or "").split(".xml")[0]
	parts = progressive_name.split("_")

	if len(parts) == 2 and parts[0] == company_tax_id and parts[1]:
		return progressive_name, parts[1]

	return get_progressive_name_and_number(frappe._dict({"company_tax_id": company_tax_id}))
