from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
from typing import Any

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint, cstr
from frappe.utils.file_manager import save_file

from erpnext.regional.italy.utils import (
	get_invoice_summary,
	get_progressive_name_and_number,
)

from fab_italy_edi.fatturapa.regional_compat import (
	get_address_country_code,
	get_attachment_progressive_name_and_number,
	prepare_e_invoice_address,
)
from erpnext.setup.doctype.company.company import get_default_company_address
from frappe.contacts.doctype.address.address import get_default_address


FOREIGN_ADDRESS_PROVINCE = "EE"
FOREIGN_ADDRESS_PINCODE = "00000"


@dataclass(slots=True)
class RenderedAutofatturaXML:
	file_url: str
	file_name: str
	content: str
	document_number: str


def render_and_attach_autofattura_xml(document, purchase_invoice, configuration) -> RenderedAutofatturaXML:
	context = build_autofattura_xml_context(document, purchase_invoice, configuration)
	invoice_xml = frappe.render_template(
		"fab_italy_edi/fatturapa/autofattura.xml",
		context=context,
		is_path=True,
	)
	invoice_xml = invoice_xml.replace("&", "&amp;")

	file_doc = save_file(
		context["xml_file_name"],
		invoice_xml.encode("utf-8"),
		document.doctype,
		document.name,
		is_private=1,
		df="generated_xml",
	)
	return RenderedAutofatturaXML(
		file_url=file_doc.file_url,
		file_name=file_doc.file_name,
		content=invoice_xml,
		document_number=context["document_number"],
	)


def build_autofattura_xml_context(document, purchase_invoice, configuration) -> dict[str, Any]:
	company = frappe.get_doc("Company", get_business_field(document, "company") or purchase_invoice.company)
	supplier = frappe.get_doc("Supplier", purchase_invoice.supplier)
	company_address = get_company_address_data(company.name)
	supplier_address = get_supplier_address_data(supplier.name)
	document_number = get_or_allocate_autofattura_document_number(document)
	progressive_name, progressive_number = get_or_allocate_progressive_name(document, configuration, company)
	lines = build_autofattura_editor_lines(document, require_complete=True)
	summaries = build_autofattura_editor_tax_summaries(document, require_complete=True)
	totals = compute_autofattura_editor_totals(document)
	if not lines:
		raise ValidationError(_("Add at least one item row before sending an autofattura to SDI."))
	if not summaries:
		raise ValidationError(_("Set the VAT rate or Natura on the autofattura lines before sending to SDI."))

	supplier_tax_country, supplier_tax_code = split_foreign_tax_identifier(
		supplier.tax_id,
		get_address_country_code(supplier_address),
	)
	if not supplier_tax_country or not supplier_tax_code:
		raise ValidationError(_("Set the foreign supplier Tax ID before sending an autofattura to SDI."))

	recipient_code = cstr(configuration.get("recipient_code_fallback") or "").strip() or "0000000"
	sender_vat_id = cstr(configuration.get("sender_vat_id") or company.tax_id or "").strip()
	sender_fiscal_code = cstr(configuration.get("sender_fiscal_code") or company.fiscal_code or "").strip()
	company_fiscal_regime = cstr(getattr(company, "fiscal_regime", "") or "").strip()
	company_fiscal_regime_code = company_fiscal_regime.split("-")[0].strip() if company_fiscal_regime else ""
	if not company_fiscal_regime_code:
		raise ValidationError(
			_("Fiscal Regime is mandatory, kindly set the fiscal regime in the company {0}").format(company.name)
		)
	if not sender_vat_id and not sender_fiscal_code:
		raise ValidationError(_("Set Sender VAT ID or Sender Fiscal Code on EDI Configuration before sending to SDI."))

	bill_no = cstr(
		get_business_field(document, "supplier_invoice_number", "autofattura_reference_invoice_number")
		or getattr(purchase_invoice, "bill_no", "")
	).strip()
	bill_date = get_business_field(
		document, "supplier_invoice_date", "autofattura_reference_invoice_date"
	) or getattr(purchase_invoice, "bill_date", None)
	if not bill_no or not bill_date:
		raise ValidationError(
			_("Set Supplier Invoice Number and Supplier Invoice Date on the autofattura editor before sending.")
		)

	return {
		"transmission_format_code": "FPR12",
		"transmitter_country_code": "IT",
		"transmitter_id_code": strip_country_prefix(sender_fiscal_code or sender_vat_id),
		"recipient_code": recipient_code,
		"sender_email": cstr(configuration.get("sender_email") or "").strip(),
		"seller": {
			"country_code": supplier_tax_country,
			"tax_id": supplier_tax_code,
			"fiscal_code": sender_fiscal_code or strip_country_prefix(company.tax_id),
			"fiscal_regime": company_fiscal_regime_code,
			"name": supplier.supplier_name or supplier.name,
			"address": build_xml_address(supplier_address, foreign=True),
		},
		"buyer": {
			"country_code": "IT",
			"tax_id": strip_country_prefix(company.tax_id),
			"fiscal_code": sender_fiscal_code or strip_country_prefix(company.tax_id),
			"name": company.company_name or company.name,
			"address": build_xml_address(company_address, foreign=False),
		},
		"document_type": get_business_field(document, "document_type", "autofattura_document_type"),
		"document_date": cstr(get_business_field(document, "document_date", "autofattura_document_date")),
		"document_number": document_number,
		"currency": cstr(get_business_field(document, "currency", "autofattura_currency") or getattr(purchase_invoice, "currency", "")),
		"grand_total": totals["grand_total"],
		"causale": build_autofattura_causale(document, purchase_invoice),
		"linked_invoice_number": bill_no,
		"linked_invoice_date": cstr(bill_date),
		"lines": lines,
		"summaries": summaries,
		"xml_file_name": f"{progressive_name}.xml",
		"progressive_number": progressive_number,
	}


def get_company_address_data(company_name: str):
	address_name = get_default_company_address(company_name)
	if not address_name:
		raise ValidationError(_("Set a primary Company address before sending an autofattura to SDI."))
	return prepare_e_invoice_address(frappe.get_doc("Address", address_name))


def get_supplier_address_data(supplier_name: str):
	address_name = get_default_address("Supplier", supplier_name)
	if not address_name:
		raise ValidationError(_("Set a primary Supplier address before sending an autofattura to SDI."))
	return prepare_e_invoice_address(frappe.get_doc("Address", address_name))


def get_or_allocate_autofattura_document_number(document) -> str:
	number_field = "document_number" if hasattr(document, "document_number") else "autofattura_document_number"
	series_field = "naming_series" if hasattr(document, "naming_series") else "autofattura_naming_series"
	existing = cstr(getattr(document, number_field, "")).strip()
	if existing:
		return existing

	number = frappe.model.naming.make_autoname(getattr(document, series_field))
	setattr(document, number_field, number)
	return number


def get_or_allocate_progressive_name(document, configuration, company) -> tuple[str, str]:
	company_tax_id = strip_country_prefix(configuration.get("sender_vat_id") or company.tax_id)
	if not company_tax_id:
		raise ValidationError(_("Set Sender VAT ID or Company Tax ID before sending an autofattura to SDI."))
	company_tax_id = f"IT{company_tax_id}"

	existing_file_name = None
	if document.generated_xml:
		existing_file_name = frappe.db.get_value("File", {"file_url": document.generated_xml}, "file_name")
	if existing_file_name:
		return get_attachment_progressive_name_and_number(existing_file_name, company_tax_id)

	holder = frappe._dict({"name": document.name, "company_tax_id": company_tax_id})
	return get_progressive_name_and_number(holder)


def build_autofattura_editor_lines(document, *, require_complete: bool = False) -> list[dict[str, Any]]:
	lines: list[dict[str, Any]] = []
	for idx, row in enumerate(get_line_rows(document), start=1):
		description = strip_html(getattr(row, "description", None)) or _("Purchase item")
		qty = to_decimal(getattr(row, "quantity", 0) or 0)
		unit_price = to_decimal(getattr(row, "unit_price", 0) or 0)
		total_price = to_decimal(getattr(row, "total_price", None) or (qty * unit_price))
		tax_rate = parse_optional_decimal(getattr(row, "tax_rate", None))
		nature = cstr(getattr(row, "nature", "")).strip() or None
		reference_law = cstr(getattr(row, "reference_law", "")).strip() or None
		if tax_rate is None and nature:
			tax_rate = Decimal("0")
		if require_complete:
			if tax_rate is None and not nature:
				raise ValidationError(
					_("Set a VAT rate or Natura for autofattura line {0} before sending.").format(idx)
				)
			if tax_rate is not None and tax_rate == 0 and not nature:
				raise ValidationError(
					_("Set Natura for autofattura line {0} when VAT rate is 0.").format(idx)
				)
		lines.append(
			{
				"idx": idx,
				"description": description,
				"qty": quantize_amount(qty, precision="0.00"),
				"uom": cstr(getattr(row, "unit_of_measure", "")).strip() or "NR",
				"unit_price": quantize_amount(unit_price),
				"total_price": quantize_amount(total_price),
				"tax_rate": quantize_amount(tax_rate) if tax_rate is not None else None,
				"nature": nature,
				"reference_law": reference_law,
			}
		)
	return lines


def build_autofattura_editor_tax_summaries(document, *, require_complete: bool = False) -> list[dict[str, Any]]:
	summary_index: dict[tuple[str, str, str], dict[str, Any]] = {}
	for line in build_autofattura_editor_lines(document, require_complete=require_complete):
		if line["tax_rate"] in (None, "") and not line["nature"]:
			continue
		rate_key = cstr(line["tax_rate"] or "0.00")
		nature = line["nature"] or ""
		reference_law = line["reference_law"] or ""
		key = (rate_key, nature, reference_law)
		entry = summary_index.setdefault(
			key,
			{
				"rate": rate_key,
				"taxable_amount": Decimal("0.00"),
				"tax_amount": Decimal("0.00"),
				"nature": nature or None,
				"law": reference_law or None,
			},
		)
		taxable_amount = to_decimal(line["total_price"])
		entry["taxable_amount"] += taxable_amount
		entry["tax_amount"] += calculate_tax_amount(taxable_amount, to_decimal(rate_key))

	return [
		{
			"rate": entry["rate"],
			"taxable_amount": quantize_amount(entry["taxable_amount"]),
			"tax_amount": quantize_amount(entry["tax_amount"]),
			"nature": entry["nature"],
			"law": entry["law"],
		}
		for _, entry in sorted(summary_index.items(), key=lambda item: item[0])
	]


def compute_autofattura_editor_totals(document) -> dict[str, str]:
	lines = build_autofattura_editor_lines(document)
	summaries = build_autofattura_editor_tax_summaries(document)
	net_total = sum((to_decimal(line["total_price"]) for line in lines), Decimal("0.00"))
	tax_total = sum((to_decimal(summary["tax_amount"]) for summary in summaries), Decimal("0.00"))
	return {
		"net_total": quantize_amount(net_total),
		"tax_total": quantize_amount(tax_total),
		"grand_total": quantize_amount(net_total + tax_total),
	}


def get_line_rows(document):
	return getattr(document, "lines", None) or getattr(document, "autofattura_lines", None) or []


def get_business_field(document, *fieldnames: str):
	for fieldname in fieldnames:
		if hasattr(document, fieldname):
			value = getattr(document, fieldname)
			if value not in (None, ""):
				return value
	return None


def build_autofattura_lines(purchase_invoice) -> list[dict[str, Any]]:
	summaries = build_autofattura_summary_index(purchase_invoice)
	if not summaries:
		return []

	lines: list[dict[str, Any]] = []
	default_zero_summary = summaries.get("0.0")
	for row in purchase_invoice.items:
		tax_rate = resolve_item_tax_rate(row)
		if tax_rate is None and len(summaries) == 1:
			tax_rate = next(iter(summaries.values()))["rate"]
		if tax_rate is None:
			raise ValidationError(
				_("Unable to determine the VAT rate for row {0}. Set an Item Tax Template or matching tax rows.").format(
					row.idx
				)
			)

		zero_summary = default_zero_summary if str(tax_rate) == "0.0" else None
		lines.append(
			{
				"idx": row.idx,
				"description": strip_html(row.description) or row.item_name or row.item_code or _("Purchase item"),
				"qty": quantize_amount(getattr(row, "qty", 0), precision="0.00"),
				"uom": cstr(row.uom or row.stock_uom or "NR").strip(),
				"unit_price": quantize_amount(getattr(row, "net_rate", None) or getattr(row, "rate", 0)),
				"total_price": quantize_amount(getattr(row, "net_amount", None) or getattr(row, "amount", 0)),
				"tax_rate": quantize_amount(tax_rate),
				"nature": zero_summary.get("nature") if zero_summary else None,
			}
		)

	return lines


def build_autofattura_tax_summaries(purchase_invoice) -> list[dict[str, Any]]:
	summary_index = build_autofattura_summary_index(purchase_invoice)
	return [summary_index[key] for key in sorted(summary_index)]


def build_autofattura_summary_index(purchase_invoice) -> dict[str, dict[str, Any]]:
	if purchase_invoice.taxes and purchase_invoice.item_wise_tax_details:
		raw_summary = get_invoice_summary(
			list(purchase_invoice.items),
			list(purchase_invoice.taxes),
			list(purchase_invoice.item_wise_tax_details),
		)
		if raw_summary:
			return {
				key: {
					"key": key,
					"rate": quantize_amount(data.get("rate", key)),
					"taxable_amount": quantize_amount(data.get("taxable_amount", 0)),
					"tax_amount": quantize_amount(data.get("tax_amount", 0)),
					"nature": cstr(data.get("tax_exemption_reason") or "").split("-")[0] or None,
					"law": cstr(data.get("tax_exemption_law") or "").strip() or None,
				}
				for key, data in normalize_raw_summary(raw_summary).items()
			}

	summary_index: dict[str, dict[str, Any]] = {}
	for row in purchase_invoice.items:
		tax_rate = resolve_item_tax_rate(row)
		if tax_rate is None:
			continue
		key = cstr(tax_rate)
		entry = summary_index.setdefault(
			key,
			{
				"key": key,
				"rate": quantize_amount(tax_rate),
				"taxable_amount": Decimal("0.00"),
				"tax_amount": Decimal("0.00"),
				"nature": None,
				"law": None,
			},
		)
		taxable_amount = to_decimal(getattr(row, "net_amount", None) or getattr(row, "amount", 0))
		entry["taxable_amount"] += taxable_amount
		entry["tax_amount"] += calculate_tax_amount(taxable_amount, tax_rate)

	for entry in summary_index.values():
		entry["taxable_amount"] = quantize_amount(entry["taxable_amount"])
		entry["tax_amount"] = quantize_amount(entry["tax_amount"])
	return summary_index


def normalize_raw_summary(raw_summary) -> dict[str, dict[str, Any]]:
	out: dict[str, dict[str, Any]] = {}
	for key, data in raw_summary.items():
		entry = dict(data)
		entry["rate"] = key
		out[key] = entry
	return out


def resolve_item_tax_rate(row) -> Decimal | None:
	item_tax_rate = getattr(row, "item_tax_rate", None)
	parsed = parse_tax_rate_json(item_tax_rate)
	if parsed is not None:
		return parsed

	item_tax_template = cstr(getattr(row, "item_tax_template", "")).strip()
	if not item_tax_template:
		return None
	template_taxes = frappe.get_cached_value("Item Tax Template", item_tax_template, "taxes") or []
	rates = [
		to_decimal(d.get("tax_rate"))
		for d in template_taxes
		if not cint(d.get("not_applicable")) and to_decimal(d.get("tax_rate")) is not None
	]
	if not rates:
		return None
	return rates[0]


def parse_tax_rate_json(value: Any) -> Decimal | None:
	if not value:
		return None
	if isinstance(value, dict):
		values = value.values()
	else:
		try:
			values = (json.loads(value) or {}).values()
		except Exception:
			return None
	for rate in values:
		decimal_rate = to_decimal(rate)
		if decimal_rate is not None:
			return decimal_rate
	return None


def parse_optional_decimal(value: Any) -> Decimal | None:
	if value in (None, ""):
		return None
	return to_decimal(value)


def split_foreign_tax_identifier(tax_id: str | None, country_code: str | None) -> tuple[str | None, str | None]:
	tax_id = cstr(tax_id or "").strip().upper()
	country_code = cstr(country_code or "").strip().upper()
	if not tax_id:
		return None, None
	if len(tax_id) > 2 and tax_id[:2].isalpha():
		return tax_id[:2], tax_id[2:]
	return country_code or None, tax_id


def build_xml_address(address, *, foreign: bool) -> dict[str, Any]:
	country_code = get_address_country_code(address)
	pincode = cstr(address.get("pincode") or "").strip()
	if foreign and len(pincode) != 5:
		pincode = FOREIGN_ADDRESS_PINCODE
	if not pincode:
		raise ValidationError(_("Address {0} is missing a postal code.").format(address.name))

	province = cstr(address.get("state_code") or "").strip().upper()
	if foreign:
		province = FOREIGN_ADDRESS_PROVINCE
	elif not province:
		raise ValidationError(_("Address {0} is missing a province/state code.").format(address.name))

	return {
		"address_line1": cstr(address.get("address_line1") or "").strip(),
		"city": cstr(address.get("city") or "").strip(),
		"pincode": pincode,
		"province": province,
		"country_code": country_code,
	}


def build_autofattura_causale(document, purchase_invoice) -> str:
	bill_no = cstr(
		get_business_field(document, "supplier_invoice_number", "autofattura_reference_invoice_number")
		or getattr(purchase_invoice, "bill_no", "")
	).strip()
	document_type = cstr(get_business_field(document, "document_type", "autofattura_document_type") or "").strip()
	if bill_no:
		return _("Autofattura {0} for supplier invoice {1}").format(document_type, bill_no)
	return _("Autofattura {0} generated from Purchase Invoice {1}").format(document_type, purchase_invoice.name)


def calculate_tax_amount(taxable_amount: Decimal, tax_rate: Decimal) -> Decimal:
	return (taxable_amount * tax_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_amount(value: Any, precision: str = "0.01") -> str:
	return format_decimal(to_decimal(value).quantize(Decimal(precision), rounding=ROUND_HALF_UP))


def format_decimal(value: Decimal) -> str:
	return f"{value:.2f}" if value.as_tuple().exponent <= -2 else format(value, "f")


def to_decimal(value: Any) -> Decimal:
	return Decimal(str(value or 0))


def strip_country_prefix(value: str | None) -> str:
	value = cstr(value or "").strip().upper()
	if value.startswith("IT"):
		return value[2:]
	return value


def strip_html(value: Any) -> str:
	return frappe.utils.strip_html(cstr(value or "")).strip()
