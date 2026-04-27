from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import nowdate

from erpnext.setup.doctype.company.company import get_default_company_address
from frappe.contacts.doctype.address.address import get_default_address

from fab_italy_edi.api import (
	prepare_document_for_outbound_queue,
	get_enabled_edi_configuration,
	get_enabled_provider,
	raise_if_document_already_transmitting,
)
from fab_italy_edi.fatturapa.autofattura import (
	build_autofattura_editor_lines,
	build_autofattura_editor_tax_summaries,
	compute_autofattura_editor_totals,
	render_and_attach_autofattura_xml,
	resolve_item_tax_rate,
)
from fab_italy_edi.polling import get_provider_adapter


AUTOFATTURA_DOCUMENT_TYPES = ("TD17", "TD18", "TD19")
AUTOFATTURA_DOCUMENT_TYPE_LABELS = {
	"TD17": _("TD17 - Purchase of services from abroad"),
	"TD18": _("TD18 - Purchase of goods from EU suppliers"),
	"TD19": _("TD19 - Purchase of goods from non-EU suppliers"),
}
AUTOFATTURA_DOCUMENT_TYPE_HELP = {
	"TD17": _("Italian taxonomy code for autofattura on services purchased from abroad."),
	"TD18": _("Italian taxonomy code for autofattura on goods purchased from EU suppliers."),
	"TD19": _("Italian taxonomy code for autofattura on goods purchased from non-EU suppliers."),
}
ITALY_COUNTRY_NAMES = {
	"italy",
	"italia",
	"italian republic",
	"repubblica italiana",
}
ACTIVE_TRANSMISSION_STATES = {"queued", "sending", "sent", "delivered", "accepted"}


@frappe.whitelist()
def get_purchase_invoice_autofattura_context(docname: str) -> dict[str, Any]:
	purchase_invoice = get_purchase_invoice_for_autofattura(docname)
	return build_autofattura_context(purchase_invoice)


@frappe.whitelist()
def confirm_autofattura_review(docname: str) -> dict[str, Any]:
	autofattura = get_autofattura(docname)
	frappe.has_permission("Autofattura", doc=autofattura, throw=True)

	notices = get_autofattura_review_notices(autofattura)
	blocking_notices = [notice for notice in notices if notice["level"] == "warning"]
	if blocking_notices:
		raise ValidationError(_("Resolve the remaining autofattura review items before confirming this draft."))

	autofattura.validation_state = "valid"
	transport = get_or_create_autofattura_transport_document(autofattura)
	transport.validation_state = "valid"
	sync_transport_document_from_autofattura(transport, autofattura)
	transport.save(ignore_permissions=True)
	sync_autofattura_from_transport_document(autofattura, transport, save=True)
	return {
		"autofattura": autofattura.name,
		"edi_document": transport.name,
		"validation_state": autofattura.validation_state,
	}


@frappe.whitelist()
def send_autofattura_to_sdi(docname: str) -> dict[str, Any]:
	autofattura = get_autofattura(docname)
	frappe.has_permission("Autofattura", doc=autofattura, throw=True)
	transport = get_or_create_autofattura_transport_document(autofattura)
	if normalize_text(autofattura.validation_state) != "valid":
		raise ValidationError(_("Confirm autofattura review before sending this draft to SDI."))
	raise_if_document_already_transmitting(transport, source_label=_("Autofattura"))

	purchase_invoice = get_purchase_invoice_for_autofattura(autofattura.source_purchase_invoice)
	blockers = collect_autofattura_send_blockers(purchase_invoice, autofattura)
	if blockers:
		raise ValidationError(_("Resolve the autofattura send blockers before sending this draft to SDI."))

	configuration = get_enabled_edi_configuration(autofattura.company)
	provider = get_enabled_provider(configuration.default_provider)
	adapter = get_provider_adapter(provider)
	if not adapter or not callable(getattr(adapter, "submit_outbound_invoice", None)):
		raise ValidationError(_("Provider {0} does not support outbound SDI submission yet.").format(provider.name))
	if callable(getattr(adapter, "ensure_outbound_submission_ready", None)):
		adapter.ensure_outbound_submission_ready(configuration=configuration, provider=provider)

	transport.channel = provider.channel
	transport.provider = provider.name
	sync_transport_document_from_autofattura(transport, autofattura)
	try:
		rendered_file = render_and_attach_autofattura_xml(autofattura, purchase_invoice, configuration)
	except Exception as exc:
		transport.validation_state = "validation_failed"
		transport.last_error = str(exc)
		transport.save(ignore_permissions=True)
		autofattura.validation_state = "validation_failed"
		autofattura.last_error = str(exc)
		autofattura.save(ignore_permissions=True)
		raise

	autofattura.generated_xml = rendered_file.file_url
	autofattura.document_number = rendered_file.document_number
	autofattura.validation_state = "valid"
	sync_transport_document_from_autofattura(transport, autofattura)
	transport.generated_xml = rendered_file.file_url
	transport.validation_state = "valid"
	result = prepare_document_for_outbound_queue(transport, rendered_file)
	return {
		"autofattura": autofattura.name,
		"edi_document": result["edi_document"],
		"external_submission_id": result["external_submission_id"],
		"transmission_state": result["transmission_state"],
		"latest_receipt_state": result["latest_receipt_state"],
		"autofattura_document_number": autofattura.document_number,
	}


@frappe.whitelist()
def prepare_autofattura_from_purchase_invoice(
	docname: str, document_type: str, document_date: str | None = None
) -> dict[str, Any]:
	purchase_invoice = get_purchase_invoice_for_autofattura(docname)
	context = build_autofattura_context(purchase_invoice)
	document_type = normalize_text(document_type)
	if document_type not in AUTOFATTURA_DOCUMENT_TYPES:
		raise ValidationError(_("Select a valid autofattura document type (TD17, TD18, or TD19)."))

	if not context["is_company_in_italy"]:
		raise ValidationError(_("Autofattura preparation is only supported for Italian companies."))
	if not context["supplier"]:
		raise ValidationError(_("Select a Supplier before preparing an autofattura."))
	if not context["is_foreign_supplier"]:
		raise ValidationError(context["reason"] or _("This supplier does not appear to be foreign."))
	if not context["autofattura_naming_series"]:
		raise ValidationError(
			_("Set Autofattura Naming Series on EDI Configuration {0} before preparing autofattura documents.").format(
				purchase_invoice.company
			)
		)
	document_date = normalize_text(document_date) or context.get("autofattura_document_date")
	document_date = document_date or normalize_text(getattr(purchase_invoice, "posting_date", None)) or nowdate()

	autofattura = (
		frappe.get_doc("Autofattura", context["autofattura"])
		if context["autofattura"]
		else frappe.new_doc("Autofattura")
	)
	autofattura.source_purchase_invoice = purchase_invoice.name
	autofattura.company = purchase_invoice.company
	autofattura.supplier = purchase_invoice.supplier
	autofattura.supplier_name = purchase_invoice.supplier_name or purchase_invoice.supplier
	autofattura.document_type = document_type
	autofattura.document_date = document_date
	autofattura.naming_series = context["autofattura_naming_series"]
	if normalize_text(autofattura.transmission_state) not in ACTIVE_TRANSMISSION_STATES:
		autofattura.validation_state = "draft"
		autofattura.transmission_state = "draft"
		autofattura.latest_receipt_state = None
	seed_autofattura_editor_from_purchase_invoice(
		autofattura,
		purchase_invoice,
		preserve_existing=bool(context["autofattura"]),
	)
	autofattura.save(ignore_permissions=True)
	transport = get_or_create_autofattura_transport_document(
		autofattura,
		legacy_docname=context["autofattura_document"],
	)
	sync_transport_document_from_autofattura(transport, autofattura)
	transport.save(ignore_permissions=True)
	sync_autofattura_from_transport_document(autofattura, transport, save=True)
	sync_purchase_invoice_links_from_autofattura(autofattura)
	purchase_invoice.add_comment(
		"Info",
		_("Prepared autofattura draft {0} as {1} using series {2}.").format(
			autofattura.name,
			document_type,
			context["autofattura_naming_series"],
		),
	)
	return {
		"autofattura": autofattura.name,
		"edi_document": transport.name,
		"document_type": document_type,
		"document_type_label": get_autofattura_document_type_label(document_type),
		"document_date": document_date,
		"naming_series": context["autofattura_naming_series"],
		"created": not bool(context["autofattura"]),
	}


@frappe.whitelist()
def get_autofattura_dashboard(docname: str | None = None) -> dict[str, Any]:
	selected_document = None
	if docname:
		selected_document = frappe.get_doc("Autofattura", docname)
		frappe.has_permission("Autofattura", doc=selected_document, throw=True)

	rows = frappe.get_list(
		"Autofattura",
		fields=[
			"name",
			"source_purchase_invoice",
			"linked_edi_document",
			"company",
			"supplier_name",
			"document_type",
			"document_date",
			"document_number",
			"naming_series",
			"validation_state",
			"transmission_state",
			"latest_receipt_state",
			"generated_xml",
			"last_error",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=20,
	)
	return {
		"selected_autofattura": serialize_autofattura_document(selected_document) if selected_document else None,
		"autofatture": [serialize_autofattura_row(row) for row in rows],
	}


def get_purchase_invoice_for_autofattura(docname: str):
	purchase_invoice = frappe.get_doc("Purchase Invoice", docname)
	frappe.has_permission("Purchase Invoice", doc=purchase_invoice, throw=True)
	return purchase_invoice


def get_autofattura(docname: str):
	autofattura = frappe.get_doc("Autofattura", docname)
	if normalize_text(getattr(autofattura, "source_purchase_invoice", None)) is None:
		raise ValidationError(_("Autofattura {0} is missing a source Purchase Invoice.").format(autofattura.name))
	return autofattura


def serialize_autofattura_document(document) -> dict[str, Any]:
	if not document:
		return {}

	transport = get_linked_autofattura_transport_document(document)
	sync_autofattura_from_transport_document(document, transport)
	totals = sync_autofattura_totals(document)
	data = serialize_autofattura_row(
		{
			"name": document.name,
			"source_doctype": "Purchase Invoice",
			"source_name": document.source_purchase_invoice,
			"linked_edi_document": document.linked_edi_document,
			"company": document.company,
			"party_name": document.supplier_name or document.supplier,
			"autofattura_document_type": document.document_type,
			"autofattura_document_date": document.document_date,
			"autofattura_document_number": document.document_number,
			"autofattura_naming_series": document.naming_series,
			"autofattura_reference_invoice_number": document.supplier_invoice_number,
			"autofattura_reference_invoice_date": document.supplier_invoice_date,
			"autofattura_currency": document.currency,
			"validation_state": document.validation_state,
			"transmission_state": document.transmission_state,
			"latest_receipt_state": document.latest_receipt_state,
			"generated_xml": document.generated_xml,
			"last_error": document.last_error,
			"modified": document.modified,
		}
	)
	data["autofattura_lines"] = build_autofattura_editor_lines(document)
	data["autofattura_tax_summaries"] = build_autofattura_editor_tax_summaries(document)
	data["autofattura_net_total"] = document.net_total or totals["net_total"]
	data["autofattura_tax_total"] = document.tax_total or totals["tax_total"]
	data["autofattura_grand_total"] = document.grand_total or totals["grand_total"]

	if document.source_purchase_invoice:
		purchase_invoice = get_purchase_invoice_for_autofattura(document.source_purchase_invoice)
		data["source_purchase_invoice"] = build_source_purchase_invoice_preview(purchase_invoice)
		data["preview_notices"] = build_autofattura_preview_notices(purchase_invoice, document)
		data["send_blockers"] = collect_autofattura_send_blockers(purchase_invoice, document)
	else:
		data["source_purchase_invoice"] = None
		data["preview_notices"] = []
		data["send_blockers"] = [(_("This autofattura draft is not linked to a source Purchase Invoice."))]

	data["preview_warnings"] = [notice["message"] for notice in data["preview_notices"] if notice["level"] == "warning"]
	data["preview_notes"] = [notice["message"] for notice in data["preview_notices"] if notice["level"] == "info"]
	data["can_confirm_review"] = not data["preview_warnings"]
	data["can_send"] = not data["send_blockers"]
	data["send_supported"] = True
	data["send_help"] = (
		_("Ready to send to SDI.")
		if data["can_send"]
		else _("Resolve the autofattura send blockers before sending to SDI.")
	)

	return data


def serialize_autofattura_row(row: dict[str, Any]) -> dict[str, Any]:
	document_type = normalize_text(row.get("autofattura_document_type"))
	return {
		**row,
		"document_type_label": get_autofattura_document_type_label(document_type),
		"document_type_help": get_autofattura_document_type_help(document_type),
	}


def build_source_purchase_invoice_preview(purchase_invoice) -> dict[str, Any]:
	return {
		"name": purchase_invoice.name,
		"supplier": getattr(purchase_invoice, "supplier", None),
		"supplier_name": getattr(purchase_invoice, "supplier_name", None),
		"posting_date": normalize_text(getattr(purchase_invoice, "posting_date", None)),
		"bill_no": normalize_text(getattr(purchase_invoice, "bill_no", None)),
		"bill_date": normalize_text(getattr(purchase_invoice, "bill_date", None)),
		"due_date": normalize_text(getattr(purchase_invoice, "due_date", None)),
		"currency": getattr(purchase_invoice, "currency", None),
		"conversion_rate": getattr(purchase_invoice, "conversion_rate", None),
		"credit_to": getattr(purchase_invoice, "credit_to", None),
		"net_total": getattr(purchase_invoice, "net_total", None),
		"base_net_total": getattr(purchase_invoice, "base_net_total", None),
		"grand_total": getattr(purchase_invoice, "grand_total", None),
		"base_grand_total": getattr(purchase_invoice, "base_grand_total", None),
		"total_taxes_and_charges": getattr(purchase_invoice, "total_taxes_and_charges", None),
		"base_total_taxes_and_charges": getattr(purchase_invoice, "base_total_taxes_and_charges", None),
		"remarks": getattr(purchase_invoice, "remarks", None),
		"items": [
			{
				"idx": getattr(row, "idx", None),
				"item_name": getattr(row, "item_name", None),
				"description": getattr(row, "description", None),
				"qty": getattr(row, "qty", None),
				"uom": getattr(row, "uom", None),
				"rate": getattr(row, "rate", None),
				"amount": getattr(row, "amount", None),
				"expense_account": getattr(row, "expense_account", None),
				"item_tax_template": getattr(row, "item_tax_template", None),
			}
			for row in (getattr(purchase_invoice, "items", None) or [])
		],
		"taxes": [
			{
				"idx": getattr(row, "idx", None),
				"description": getattr(row, "description", None),
				"charge_type": getattr(row, "charge_type", None),
				"account_head": getattr(row, "account_head", None),
				"rate": getattr(row, "rate", None),
				"tax_amount": getattr(row, "tax_amount", None),
			}
			for row in (getattr(purchase_invoice, "taxes", None) or [])
		],
	}


def build_autofattura_preview_notices(purchase_invoice, document) -> list[dict[str, Any]]:
	notices = []
	if not normalize_text(getattr(purchase_invoice, "supplier", None)):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Select a Supplier on the source Purchase Invoice."),
				action="open_purchase_invoice",
			)
		)
	if not normalize_text(getattr(purchase_invoice, "credit_to", None)):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Set the payable account on the source Purchase Invoice."),
				action="open_purchase_invoice",
			)
		)
	if not any(
		normalize_text(getattr(row, "expense_account", None))
		for row in (getattr(purchase_invoice, "items", None) or [])
	):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Every source item row should have an expense account before final generation."),
				action="open_purchase_invoice",
			)
		)
	notices.append(
		build_autofattura_notice(
			"info",
			_(
				"Purchase Invoice tax rows are not used to generate autofattura VAT. Edit the autofattura lines in the editor instead."
			),
			action="open_autofattura_editor",
		)
	)
	if not normalize_text(getattr(document, "document_date", None)):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Set the autofattura document date before final generation."),
				action="edit_autofattura",
			)
		)
	if not (getattr(document, "lines", None) or []):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Add at least one autofattura line in the editor before confirming this draft."),
				action="open_autofattura_editor",
			)
		)
	if get_autofattura_line_tax_warnings(document):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Complete the VAT rate or Natura on every autofattura line before confirming this draft."),
				action="open_autofattura_editor",
			)
		)
	if not normalize_text(getattr(document, "supplier_invoice_number", None)):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Set the supplier invoice number on the autofattura editor."),
				action="open_autofattura_editor",
			)
		)
	if not normalize_text(getattr(document, "supplier_invoice_date", None)):
		notices.append(
			build_autofattura_notice(
				"warning",
				_("Set the supplier invoice date on the autofattura editor."),
				action="open_autofattura_editor",
			)
		)
	return notices


def build_autofattura_notice(level: str, message: str, action: str | None = None) -> dict[str, Any]:
	return {
		"level": level,
		"message": message,
		"action": action,
	}


def get_autofattura_review_notices(document) -> list[dict[str, Any]]:
	if not getattr(document, "source_purchase_invoice", None):
		return []

	purchase_invoice = get_purchase_invoice_for_autofattura(document.source_purchase_invoice)
	return build_autofattura_preview_notices(purchase_invoice, document)


def collect_autofattura_send_blockers(purchase_invoice, document) -> list[str]:
	blockers: list[str] = []
	if normalize_text(document.validation_state) != "valid":
		blockers.append(_("Confirm autofattura review before sending to SDI."))
	if normalize_text(document.transmission_state) in ACTIVE_TRANSMISSION_STATES:
		blockers.append(_("This autofattura is already queued or sent to SDI."))
	if not normalize_text(getattr(document, "document_date", None)):
		blockers.append(_("Set the autofattura document date."))
	if not normalize_text(getattr(document, "supplier_invoice_number", None)):
		blockers.append(_("Set the supplier invoice number on the autofattura editor."))
	if not normalize_text(getattr(document, "supplier_invoice_date", None)):
		blockers.append(_("Set the supplier invoice date on the autofattura editor."))
	if not normalize_text(getattr(purchase_invoice, "supplier", None)):
		blockers.append(_("Select a Supplier on the source Purchase Invoice."))
	else:
		supplier_tax_id = normalize_text(frappe.get_cached_value("Supplier", purchase_invoice.supplier, "tax_id"))
		if not supplier_tax_id:
			blockers.append(_("Set the foreign supplier Tax ID on Supplier {0}.").format(purchase_invoice.supplier))
		if not get_default_address("Supplier", purchase_invoice.supplier):
			blockers.append(_("Set a primary Supplier address on {0}.").format(purchase_invoice.supplier))
	if not get_default_company_address(purchase_invoice.company):
		blockers.append(_("Set a primary Company address on {0}.").format(purchase_invoice.company))
	if not (getattr(document, "lines", None) or []):
		blockers.append(_("Add at least one autofattura line in the autofattura editor."))
	if get_autofattura_line_tax_warnings(document):
		blockers.extend(get_autofattura_line_tax_warnings(document))
	if not build_autofattura_has_tax_basis(document):
		blockers.append(_("Set the VAT rate or Natura on the autofattura lines so FAB can generate VAT totals."))
	return deduplicate_messages(blockers)


def build_autofattura_has_tax_basis(document) -> bool:
	try:
		return bool(build_autofattura_editor_tax_summaries(document))
	except Exception:
		return False


def deduplicate_messages(messages: list[str]) -> list[str]:
	seen = set()
	ordered: list[str] = []
	for message in messages:
		if message in seen:
			continue
		seen.add(message)
		ordered.append(message)
	return ordered


def seed_autofattura_editor_from_purchase_invoice(document, purchase_invoice, *, preserve_existing: bool = True):
	fieldname = "lines" if hasattr(document, "lines") else "autofattura_lines"
	existing_lines = list(getattr(document, fieldname, None) or [])
	if not preserve_existing or not existing_lines:
		lines = []
		for row in getattr(purchase_invoice, "items", None) or []:
			tax_rate = resolve_item_tax_rate(row)
			lines.append(
				{
					"description": normalize_text(getattr(row, "description", None))
					or normalize_text(getattr(row, "item_name", None))
					or normalize_text(getattr(row, "item_code", None))
					or _("Purchase item"),
					"quantity": getattr(row, "qty", 0) or 0,
					"unit_of_measure": normalize_text(getattr(row, "uom", None))
					or normalize_text(getattr(row, "stock_uom", None))
					or "NR",
					"unit_price": getattr(row, "net_rate", None) or getattr(row, "rate", 0) or 0,
					"total_price": getattr(row, "net_amount", None) or getattr(row, "amount", 0) or 0,
					"tax_rate": float(tax_rate) if tax_rate is not None else None,
					"nature": None,
					"reference_law": None,
				}
			)
		set_document_rows(document, fieldname, lines)

	if hasattr(document, "supplier_invoice_number"):
		if not normalize_text(getattr(document, "supplier_invoice_number", None)):
			document.supplier_invoice_number = normalize_text(getattr(purchase_invoice, "bill_no", None))
		if not normalize_text(getattr(document, "supplier_invoice_date", None)):
			document.supplier_invoice_date = normalize_text(getattr(purchase_invoice, "bill_date", None))
		if not normalize_text(getattr(document, "currency", None)):
			document.currency = normalize_text(getattr(purchase_invoice, "currency", None))
	else:
		if not normalize_text(getattr(document, "autofattura_reference_invoice_number", None)):
			document.autofattura_reference_invoice_number = normalize_text(getattr(purchase_invoice, "bill_no", None))
		if not normalize_text(getattr(document, "autofattura_reference_invoice_date", None)):
			document.autofattura_reference_invoice_date = normalize_text(getattr(purchase_invoice, "bill_date", None))
		if not normalize_text(getattr(document, "autofattura_currency", None)):
			document.autofattura_currency = normalize_text(getattr(purchase_invoice, "currency", None))

	if hasattr(document, "lines"):
		sync_autofattura_totals(document)
	else:
		sync_autofattura_editor_totals(document)


def set_document_rows(document, fieldname: str, rows: list[dict[str, Any]]):
	if callable(getattr(document, "set", None)):
		document.set(fieldname, rows)
		return
	setattr(document, fieldname, [SimpleNamespace(**row) for row in rows])


def sync_autofattura_editor_totals(document) -> dict[str, str]:
	totals = compute_autofattura_editor_totals(document)
	document.autofattura_net_total = totals["net_total"]
	document.autofattura_tax_total = totals["tax_total"]
	document.autofattura_grand_total = totals["grand_total"]
	return totals


def sync_autofattura_totals(document) -> dict[str, str]:
	totals = compute_autofattura_editor_totals(document)
	document.net_total = totals["net_total"]
	document.tax_total = totals["tax_total"]
	document.grand_total = totals["grand_total"]
	return totals


def get_autofattura_line_tax_warnings(document) -> list[str]:
	warnings: list[str] = []
	for line in build_autofattura_editor_lines(document):
		rate = line.get("tax_rate")
		nature = normalize_text(line.get("nature"))
		if rate in (None, "") and not nature:
			warnings.append(
				_("Set a VAT rate or Natura for autofattura line {0}.").format(line.get("idx") or "?")
			)
			continue
		if rate not in (None, "") and float(rate) == 0.0 and not nature:
			warnings.append(
				_("Set Natura for autofattura line {0} when VAT rate is 0.").format(line.get("idx") or "?")
			)
	return warnings


def get_linked_autofattura_transport_document(autofattura):
	docname = normalize_text(getattr(autofattura, "linked_edi_document", None))
	if docname and frappe.db.exists("EDI Document", docname):
		return frappe.get_doc("EDI Document", docname)
	return None


def get_or_create_autofattura_transport_document(autofattura, legacy_docname: str | None = None):
	docname = (
		normalize_text(getattr(autofattura, "linked_edi_document", None))
		or normalize_text(legacy_docname)
		or normalize_text(
			frappe.db.get_value(
				"EDI Document",
				{
					"document_kind": "autofattura",
					"source_doctype": "Autofattura",
					"source_name": autofattura.name,
				},
				"name",
			)
		)
		or normalize_text(
			frappe.db.get_value(
				"EDI Document",
				{
					"document_kind": "autofattura",
					"source_doctype": "Purchase Invoice",
					"source_name": autofattura.source_purchase_invoice,
				},
				"name",
			)
		)
	)
	document = frappe.get_doc("EDI Document", docname) if docname else frappe.new_doc("EDI Document")
	if not normalize_text(getattr(document, "validation_state", None)):
		document.validation_state = "draft"
	if not normalize_text(getattr(document, "transmission_state", None)):
		document.transmission_state = "draft"
	sync_transport_document_from_autofattura(document, autofattura)
	return document


def sync_transport_document_from_autofattura(document, autofattura):
	document.source_doctype = "Autofattura"
	document.source_name = autofattura.name
	document.company = autofattura.company
	document.document_kind = "autofattura"
	document.party_name = autofattura.supplier_name or autofattura.supplier
	document.idempotency_key = f"autofattura:{autofattura.name}"
	document.canonical_identifier = document.canonical_identifier or build_autofattura_identifier(
		autofattura.source_purchase_invoice, autofattura.document_type
	)
	document.autofattura_document_type = autofattura.document_type
	document.autofattura_document_date = autofattura.document_date
	document.autofattura_document_number = autofattura.document_number
	document.autofattura_naming_series = autofattura.naming_series
	document.autofattura_reference_invoice_number = autofattura.supplier_invoice_number
	document.autofattura_reference_invoice_date = autofattura.supplier_invoice_date
	document.autofattura_currency = autofattura.currency
	document.generated_xml = autofattura.generated_xml
	document.last_error = autofattura.last_error
	set_document_rows(document, "autofattura_lines", build_raw_line_payloads(autofattura))
	sync_autofattura_editor_totals(document)


def sync_autofattura_from_transport_document(autofattura, document, *, save: bool = False):
	if not document:
		return autofattura
	updates = {
		"linked_edi_document": document.name,
		"validation_state": document.validation_state,
		"transmission_state": document.transmission_state,
		"latest_receipt_state": document.latest_receipt_state,
		"generated_xml": document.generated_xml,
		"last_error": document.last_error,
	}
	if normalize_text(getattr(document, "autofattura_document_number", None)):
		updates["document_number"] = document.autofattura_document_number

	for fieldname, value in updates.items():
		setattr(autofattura, fieldname, value)

	target = autofattura
	if (
		save
		and getattr(autofattura, "doctype", None) == "Autofattura"
		and normalize_text(getattr(autofattura, "name", None))
		and frappe.db.exists("Autofattura", autofattura.name)
	):
		target = frappe.get_doc("Autofattura", autofattura.name)
		for fieldname, value in updates.items():
			setattr(target, fieldname, value)
	if save:
		target.save(ignore_permissions=True)
	return target


def build_raw_line_payloads(source) -> list[dict[str, Any]]:
	fieldname = "lines" if hasattr(source, "lines") else "autofattura_lines"
	return [
		{
			"description": getattr(row, "description", None),
			"quantity": getattr(row, "quantity", None),
			"unit_of_measure": getattr(row, "unit_of_measure", None),
			"unit_price": getattr(row, "unit_price", None),
			"total_price": getattr(row, "total_price", None),
			"tax_rate": getattr(row, "tax_rate", None),
			"nature": getattr(row, "nature", None),
			"reference_law": getattr(row, "reference_law", None),
		}
		for row in (getattr(source, fieldname, None) or [])
	]


def build_autofattura_context(purchase_invoice) -> dict[str, Any]:
	company_country = normalize_text(frappe.get_cached_value("Company", purchase_invoice.company, "country"))
	supplier = normalize_text(purchase_invoice.supplier)
	supplier_country = resolve_supplier_country(supplier)
	supplier_tax_id = normalize_text(frappe.get_cached_value("Supplier", supplier, "tax_id")) if supplier else None
	autofattura = normalize_text(frappe.get_cached_value("Purchase Invoice", purchase_invoice.name, "fab_edi_autofattura")) or get_existing_autofattura_name(purchase_invoice.name)
	autofattura_document = normalize_text(
		frappe.get_cached_value("Purchase Invoice", purchase_invoice.name, "fab_edi_autofattura_document")
	) or get_existing_autofattura_document_name(purchase_invoice.name)
	autofattura_document_type = (
		normalize_text(frappe.get_cached_value("Autofattura", autofattura, "document_type"))
		if autofattura
		else None
	)
	autofattura_document_date = (
		normalize_text(frappe.get_cached_value("Autofattura", autofattura, "document_date"))
		if autofattura
		else None
	)
	config_exists = bool(frappe.db.exists("EDI Configuration", purchase_invoice.company))
	autofattura_naming_series = (
		normalize_text(
			frappe.get_cached_value("EDI Configuration", purchase_invoice.company, "autofattura_naming_series")
		)
		if config_exists
		else None
	)
	is_company_in_italy = is_italy_country(company_country)
	is_foreign_supplier = looks_like_foreign_supplier(supplier_country, supplier_tax_id)

	reason = None
	requires_naming_series = False
	can_prepare = False
	if not is_company_in_italy:
		reason = _("Autofattura preparation is only available for companies based in Italy.")
	elif not supplier:
		reason = _("Select a Supplier before preparing an autofattura.")
	elif not is_foreign_supplier:
		reason = _(
			"Set a foreign supplier country or a non-Italian supplier Tax ID before preparing an autofattura."
		)
	elif not autofattura_naming_series:
		requires_naming_series = True
		reason = _(
			"Configure a dedicated Autofattura Naming Series on EDI Configuration {0} before preparing this document."
		).format(purchase_invoice.company)
	else:
		can_prepare = True

	return {
		"purchase_invoice": purchase_invoice.name,
		"supplier": supplier,
		"supplier_country": supplier_country,
		"supplier_tax_id": supplier_tax_id,
		"company_country": company_country,
		"is_company_in_italy": is_company_in_italy,
		"is_foreign_supplier": is_foreign_supplier,
		"autofattura": autofattura,
		"autofattura_document": autofattura_document,
		"autofattura_document_type": autofattura_document_type,
		"autofattura_document_date": autofattura_document_date,
		"autofattura_naming_series": autofattura_naming_series,
		"config_exists": config_exists,
		"requires_naming_series": requires_naming_series,
		"can_prepare": can_prepare,
		"reason": reason,
	}


def get_existing_autofattura_name(purchase_invoice_name: str | None) -> str | None:
	purchase_invoice_name = normalize_text(purchase_invoice_name)
	if not purchase_invoice_name:
		return None
	return normalize_text(
		frappe.db.get_value(
			"Autofattura",
			{"source_purchase_invoice": purchase_invoice_name},
			"name",
		)
	)


def get_existing_autofattura_document_name(purchase_invoice_name: str | None) -> str | None:
	purchase_invoice_name = normalize_text(purchase_invoice_name)
	if not purchase_invoice_name:
		return None
	autofattura = get_existing_autofattura_name(purchase_invoice_name)
	if autofattura:
		return normalize_text(frappe.get_cached_value("Autofattura", autofattura, "linked_edi_document"))
	return normalize_text(
		frappe.db.get_value(
			"EDI Document",
			{
				"source_doctype": "Purchase Invoice",
				"source_name": purchase_invoice_name,
				"document_kind": "autofattura",
			},
			"name",
		)
	)


def sync_linked_autofattura_from_edi_document(document, _method: str | None = None):
	if normalize_text(getattr(document, "document_kind", None)) != "autofattura":
		return
	if normalize_text(getattr(document, "source_doctype", None)) != "Autofattura":
		return
	autofattura_name = normalize_text(getattr(document, "source_name", None))
	if not autofattura_name or not frappe.db.exists("Autofattura", autofattura_name):
		return
	autofattura = frappe.get_doc("Autofattura", autofattura_name)
	sync_autofattura_from_transport_document(autofattura, document, save=True)
	sync_purchase_invoice_links_from_autofattura(autofattura)


def sync_purchase_invoice_links_from_autofattura(autofattura, _method: str | None = None):
	purchase_invoice_name = normalize_text(getattr(autofattura, "source_purchase_invoice", None))
	if not purchase_invoice_name or not frappe.db.exists("Purchase Invoice", purchase_invoice_name):
		return

	frappe.db.set_value(
		"Purchase Invoice",
		purchase_invoice_name,
		"fab_edi_autofattura",
		autofattura.name,
		update_modified=False,
	)

	linked_edi_document = normalize_text(getattr(autofattura, "linked_edi_document", None))
	if linked_edi_document:
		frappe.db.set_value(
			"Purchase Invoice",
			purchase_invoice_name,
			"fab_edi_autofattura_document",
			linked_edi_document,
			update_modified=False,
		)


def backfill_autofatture():
	rows = frappe.get_all(
		"EDI Document",
		filters={"document_kind": "autofattura"},
		fields=["name", "source_doctype", "source_name"],
	)
	for row in rows:
		document = frappe.get_doc("EDI Document", row["name"])
		try:
			if normalize_text(document.source_doctype) == "Autofattura" and frappe.db.exists("Autofattura", document.source_name):
				autofattura = frappe.get_doc("Autofattura", document.source_name)
			else:
				purchase_invoice_name = (
					document.source_name
					if normalize_text(document.source_doctype) == "Purchase Invoice"
					else normalize_text(
						frappe.db.get_value("Autofattura", document.source_name, "source_purchase_invoice")
					)
				)
				if not purchase_invoice_name:
					continue
				purchase_invoice = frappe.get_doc("Purchase Invoice", purchase_invoice_name)
				autofattura_name = get_existing_autofattura_name(purchase_invoice_name)
				autofattura = frappe.get_doc("Autofattura", autofattura_name) if autofattura_name else frappe.new_doc("Autofattura")
				autofattura.source_purchase_invoice = purchase_invoice.name
				autofattura.company = purchase_invoice.company
				autofattura.supplier = purchase_invoice.supplier
				autofattura.supplier_name = purchase_invoice.supplier_name or purchase_invoice.supplier
				autofattura.document_type = normalize_text(getattr(document, "autofattura_document_type", None)) or "TD17"
				autofattura.document_date = normalize_text(getattr(document, "autofattura_document_date", None)) or normalize_text(getattr(purchase_invoice, "posting_date", None)) or nowdate()
				autofattura.naming_series = normalize_text(getattr(document, "autofattura_naming_series", None))
				autofattura.document_number = normalize_text(getattr(document, "autofattura_document_number", None))
				autofattura.supplier_invoice_number = normalize_text(getattr(document, "autofattura_reference_invoice_number", None)) or normalize_text(getattr(purchase_invoice, "bill_no", None))
				autofattura.supplier_invoice_date = normalize_text(getattr(document, "autofattura_reference_invoice_date", None)) or normalize_text(getattr(purchase_invoice, "bill_date", None))
				autofattura.currency = normalize_text(getattr(document, "autofattura_currency", None)) or normalize_text(getattr(purchase_invoice, "currency", None))
				autofattura.linked_edi_document = document.name
				legacy_lines = build_raw_line_payloads(document)
				if legacy_lines:
					set_document_rows(autofattura, "lines", legacy_lines)
					sync_autofattura_totals(autofattura)
				else:
					seed_autofattura_editor_from_purchase_invoice(autofattura, purchase_invoice, preserve_existing=False)
				sync_autofattura_from_transport_document(autofattura, document)
				autofattura.save(ignore_permissions=True)

			document.source_doctype = "Autofattura"
			document.source_name = autofattura.name
			sync_transport_document_from_autofattura(document, autofattura)
			document.save(ignore_permissions=True)
			sync_autofattura_from_transport_document(autofattura, document, save=True)
			sync_purchase_invoice_links_from_autofattura(autofattura)
		except Exception:
			frappe.log_error(title=_("Unable to backfill autofattura documents"), message=frappe.get_traceback())


def get_autofattura_document_type_label(document_type: str | None) -> str:
	document_type = normalize_text(document_type)
	return AUTOFATTURA_DOCUMENT_TYPE_LABELS.get(document_type, document_type or "")


def get_autofattura_document_type_help(document_type: str | None) -> str:
	document_type = normalize_text(document_type)
	return AUTOFATTURA_DOCUMENT_TYPE_HELP.get(document_type, "")


def resolve_supplier_country(supplier_name: str | None) -> str | None:
	supplier_name = normalize_text(supplier_name)
	if not supplier_name:
		return None

	supplier_country = normalize_text(frappe.get_cached_value("Supplier", supplier_name, "country"))
	if supplier_country:
		return supplier_country

	address_name = frappe.db.get_value(
		"Dynamic Link",
		{
			"parenttype": "Address",
			"link_doctype": "Supplier",
			"link_name": supplier_name,
		},
		"parent",
	)
	if address_name:
		return normalize_text(frappe.get_cached_value("Address", address_name, "country"))
	return None


def looks_like_foreign_supplier(supplier_country: str | None, supplier_tax_id: str | None) -> bool:
	if normalize_text(supplier_country):
		return not is_italy_country(supplier_country)

	supplier_tax_id = (normalize_text(supplier_tax_id) or "").upper()
	if supplier_tax_id:
		return not supplier_tax_id.startswith("IT")

	return False


def is_italy_country(country_name: str | None) -> bool:
	return (normalize_text(country_name) or "").casefold() in ITALY_COUNTRY_NAMES


def build_autofattura_identifier(purchase_invoice_name: str, document_type: str) -> str:
	return f"AUTOFATTURA:{document_type}:{purchase_invoice_name}"


def normalize_text(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None
