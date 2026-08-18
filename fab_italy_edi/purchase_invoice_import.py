from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping
import xml.etree.ElementTree as ET

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import flt, getdate, is_valid_iban, nowdate

from fab_italy_edi.inbound_tax_setup import ensure_inbound_natura_account_enabled

try:
	from fab_banks_import.bank_directory import get_bank_record_from_abi, resolve_bank_name_from_abi
except ImportError:
	def get_bank_record_from_abi(abi_code: str | None) -> dict[str, str] | None:
		return None

	def resolve_bank_name_from_abi(abi_code: str | None) -> str | None:
		return None


_CAMEL_BOUNDARY_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_RE_2 = re.compile(r"([a-z0-9])([A-Z])")
INBOUND_SUPPLIER_PREVIEW_FIELD = "fab_edi_supplier_preview_json"
INBOUND_SUPPLIER_PAYMENTS_FIELD = "fab_edi_payments_preview_json"
IMPORTED_EDI_BANK_NAME = "Imported EDI Bank"
QUARANTINED_INBOUND_SUPPLIER_PREFIX = "Quarantined stale inbound supplier placeholder"


@frappe.whitelist()
def get_incoming_supplier_invoice_preview(docname: str) -> dict[str, Any]:
	document = get_incoming_supplier_document(docname)
	return build_incoming_supplier_invoice_preview(document)


@frappe.whitelist()
def create_purchase_invoice_draft_from_edi_document(
	docname: str, supplier: str | None = None, tax_account: str | None = None
) -> dict[str, Any]:
	document = get_incoming_supplier_document(docname)
	frappe.has_permission("Purchase Invoice", ptype="create", throw=True)

	preview = build_incoming_supplier_invoice_preview(document)
	return ensure_purchase_invoice_review_draft(
		document,
		preview=preview,
		supplier=supplier,
		tax_account=tax_account,
	)


def ensure_purchase_invoice_review_draft(
	document,
	*,
	preview: dict[str, Any] | None = None,
	supplier: str | None = None,
	tax_account: str | None = None,
) -> dict[str, Any]:
	if is_quarantined_incoming_supplier_document(document):
		raise ValidationError(
			_("EDI Document {0} is a quarantined stale inbound placeholder and cannot create a Purchase Invoice.").format(
				document.name
			)
		)
	preview = preview or build_incoming_supplier_invoice_preview(document)
	existing_purchase_invoice = find_linked_purchase_invoice(document.name)
	supplier_name = normalize_text(supplier) or preview.get("default_supplier")
	if supplier_name and not frappe.db.exists("Supplier", supplier_name):
		raise ValidationError(_("Select an existing Supplier before creating the Purchase Invoice draft."))

	if existing_purchase_invoice:
		pi = frappe.get_doc("Purchase Invoice", existing_purchase_invoice)
		changed = False
		if supplier_name and pi.supplier != supplier_name:
			pi.supplier = supplier_name
			changed = True
		if (preview.get("supplier") or {}).get("display_name") and pi.supplier_name != preview["supplier"]["display_name"]:
			pi.supplier_name = preview["supplier"]["display_name"]
			changed = True
		tax_rows, unresolved_tax_buckets = build_purchase_invoice_taxes(
			preview,
			company=document.company,
			tax_account=tax_account,
			allow_unmapped=True,
		)
		if (tax_rows or normalize_text(tax_account)) and build_purchase_tax_signature(
			pi.get("taxes") or []
		) != build_purchase_tax_signature(tax_rows):
			pi.set("taxes", tax_rows)
			changed = True
		expected_remarks = build_purchase_invoice_remarks(
			preview,
			unresolved_tax_buckets=unresolved_tax_buckets,
		)
		if (pi.remarks or "") != expected_remarks:
			pi.remarks = expected_remarks
			changed = True
		prepare_purchase_invoice_exact_taxes(
			pi,
			preview,
			company=document.company,
			tax_account=tax_account,
		)
		changed = True
		if changed:
			pi.flags.ignore_mandatory = True
			pi.save(ignore_permissions=True)
		sync_edi_document_purchase_invoice_link(document, pi)
		return {"purchase_invoice": pi.name, "created": False}

	payload = build_purchase_invoice_payload(
		document,
		preview,
		supplier=supplier_name,
		tax_account=tax_account,
	)
	pi = frappe.get_doc(payload)
	prepare_purchase_invoice_exact_taxes(
		pi,
		preview,
		company=document.company,
		tax_account=tax_account,
	)
	pi.insert(ignore_permissions=True, ignore_mandatory=True)
	sync_edi_document_purchase_invoice_link(document, pi)
	document.add_comment(
		"Comment",
		_("Created Purchase Invoice draft {0} from this inbound supplier invoice.").format(pi.name),
	)
	return {"purchase_invoice": pi.name, "created": True}


def get_incoming_supplier_document(docname: str):
	document = frappe.get_doc("EDI Document", docname)
	frappe.has_permission("EDI Document", doc=document, throw=True)
	if document.document_kind != "supplier_invoice_import":
		raise ValidationError(_("EDI Document {0} is not an inbound supplier invoice.").format(docname))
	if is_quarantined_incoming_supplier_document(document):
		raise ValidationError(
			_("EDI Document {0} is a quarantined stale inbound placeholder and cannot be reviewed.").format(
				docname
			)
		)
	return document


def is_quarantined_incoming_supplier_document(document) -> bool:
	if normalize_text(getattr(document, "document_kind", None)) != "supplier_invoice_import":
		return False
	last_error = normalize_text(getattr(document, "last_error", None)) or ""
	return last_error.startswith(QUARANTINED_INBOUND_SUPPLIER_PREFIX)


def build_incoming_supplier_invoice_preview(document) -> dict[str, Any]:
	source = load_incoming_supplier_invoice_source(document)
	preview = parse_supplier_invoice_source(source)
	tax_mapping_status = get_inbound_tax_mapping_status(document.company, preview.get("taxes") or [])
	preview["edi_document"] = document.name
	preview["canonical_identifier"] = document.canonical_identifier
	preview["source_xml"] = document.source_xml
	preview["purchase_invoice"] = find_linked_purchase_invoice(document.name)
	preview["existing_suppliers"] = find_matching_suppliers(preview.get("supplier") or {})
	preview["default_supplier"] = (
		preview["existing_suppliers"][0]["name"] if len(preview["existing_suppliers"]) == 1 else None
	)
	preview["supplier_route_options"] = build_supplier_route_options(preview.get("supplier") or {})
	preview["requires_tax_account"] = any(
		abs(flt(tax.get("tax_amount"))) > 0.0001 for tax in preview.get("taxes") or []
	)
	preview["resolved_tax_buckets"] = tax_mapping_status["resolved"]
	preview["unresolved_tax_buckets"] = tax_mapping_status["unresolved"]
	preview["has_complete_tax_mapping"] = not tax_mapping_status["unresolved"]
	return preview


def build_purchase_invoice_payload(
	document,
	preview: Mapping[str, Any],
	*,
	supplier: str | None,
	tax_account: str | None,
) -> dict[str, Any]:
	item_rows = build_purchase_invoice_items(preview, company=document.company)
	if not item_rows:
		raise ValidationError(_("The inbound supplier invoice does not contain any importable lines."))

	invoice = preview["invoice"]
	tax_rows, unresolved_tax_buckets = build_purchase_invoice_taxes(
		preview,
		company=document.company,
		tax_account=tax_account,
		allow_unmapped=True,
	)
	return {
		"doctype": "Purchase Invoice",
		"company": document.company,
		"supplier": supplier,
		"supplier_name": (preview.get("supplier") or {}).get("display_name"),
		"posting_date": invoice.get("bill_date") or nowdate(),
		"bill_no": invoice.get("bill_no"),
		"bill_date": invoice.get("bill_date"),
		"due_date": invoice.get("due_date"),
		"currency": invoice.get("currency"),
		"is_return": 1 if invoice.get("is_return") else 0,
		"disable_rounded_total": 1,
		"remarks": build_purchase_invoice_remarks(
			preview,
			unresolved_tax_buckets=unresolved_tax_buckets,
		),
		"items": item_rows,
		"taxes": tax_rows,
		"fab_edi_document": document.name,
		"fab_edi_imported": 1,
		"fab_edi_source_xml": document.source_xml,
	}


def sync_edi_document_purchase_invoice_link(document, purchase_invoice) -> None:
	changed = False
	if document.source_doctype != "Purchase Invoice":
		document.source_doctype = "Purchase Invoice"
		changed = True
	if document.source_name != purchase_invoice.name:
		document.source_name = purchase_invoice.name
		changed = True
	if changed:
		document.save(ignore_permissions=True)


def find_linked_purchase_invoice(edi_document_name: str) -> str | None:
	return frappe.db.get_value("Purchase Invoice", {"fab_edi_document": edi_document_name}, "name")


def load_incoming_supplier_invoice_source(document) -> Any:
	if normalize_text(document.source_xml):
		return read_site_file_with_fallbacks(document.source_xml)

	for receipt in reversed(list(document.receipts or [])):
		payload_file = normalize_text(getattr(receipt, "payload_file", None))
		if not payload_file:
			continue
		content = read_site_file_with_fallbacks(payload_file)
		try:
			return json.loads(content)
		except ValueError:
			if content.lstrip().startswith("<"):
				return content

	raise ValidationError(
		_("EDI Document {0} does not contain an inbound payload that can be reviewed or imported.").format(
			document.name
		)
	)


def parse_supplier_invoice_source(source: Any) -> dict[str, Any]:
	if isinstance(source, str) and source.lstrip().startswith("<"):
		root = ET.fromstring(source)
		normalized = normalize_structure({normalize_key(root.tag): element_to_mapping(root)})
	elif isinstance(source, Mapping):
		payload = source.get("payload") if isinstance(source.get("payload"), Mapping) else source
		if isinstance(source.get("payload"), str) and str(source.get("payload")).lstrip().startswith("<"):
			return parse_supplier_invoice_source(source["payload"])
		normalized = normalize_structure(payload)
	else:
		raise ValidationError(_("Unsupported inbound supplier invoice payload format."))

	root = as_mapping(normalized.get("fattura_elettronica") or normalized)
	header = as_mapping(root.get("fattura_elettronica_header"))
	body = as_mapping(first(root.get("fattura_elettronica_body")))
	document_data = as_mapping(get_path(body, "dati_generali", "dati_generali_documento"))
	items = [
		build_item_preview(line)
		for line in ensure_list(get_path(body, "dati_beni_servizi", "dettaglio_linee"))
		if isinstance(line, Mapping)
	]
	taxes = [
		build_tax_preview(row)
		for row in ensure_list(get_path(body, "dati_beni_servizi", "dati_riepilogo"))
		if isinstance(row, Mapping)
	]
	payments = [
		build_payment_preview(row)
		for payment_block in ensure_list(body.get("dati_pagamento"))
		for row in ensure_list(as_mapping(payment_block).get("dettaglio_pagamento"))
		if isinstance(row, Mapping)
	]
	attachments = [
		build_attachment_preview(row)
		for row in ensure_list(body.get("allegati"))
		if isinstance(row, Mapping)
	]

	total_tax_amount = sum(flt(row.get("tax_amount")) for row in taxes)
	total_net_amount = sum(flt(row.get("taxable_amount")) for row in taxes) or sum(
		flt(item.get("amount")) for item in items
	)
	total_amount = flt(document_data.get("importo_totale_documento")) or (total_net_amount + total_tax_amount)
	due_date = max((payment.get("due_date") for payment in payments if payment.get("due_date")), default=None)
	document_type = normalize_text(document_data.get("tipo_documento"))

	return {
		"supplier": build_supplier_preview(header),
		"invoice": {
			"document_type": document_type,
			"bill_no": normalize_text(document_data.get("numero")),
			"bill_date": normalize_date(document_data.get("data")),
			"currency": normalize_text(document_data.get("divisa")) or frappe.defaults.get_global_default("currency"),
			"total_amount": total_amount,
			"total_net_amount": total_net_amount,
			"total_tax_amount": total_tax_amount,
			"due_date": due_date,
			"progressive_send": normalize_text(get_path(header, "dati_trasmissione", "progressivo_invio")),
			"destination_code": normalize_text(get_path(header, "dati_trasmissione", "codice_destinatario")),
			"notes": [note for note in ensure_list(document_data.get("causale")) if normalize_text(note)],
			"is_return": document_type == "TD04" or total_amount < 0,
		},
		"items": items,
		"taxes": taxes,
		"payments": payments,
		"attachments": attachments,
	}


def build_supplier_preview(header: Mapping[str, Any]) -> dict[str, Any]:
	supplier_party = as_mapping(header.get("cedente_prestatore"))
	transmission = as_mapping(header.get("dati_trasmissione"))
	anagrafici = as_mapping(supplier_party.get("dati_anagrafici"))
	anagrafica = as_mapping(anagrafici.get("anagrafica"))
	sede = as_mapping(supplier_party.get("sede"))
	contacts = as_mapping(supplier_party.get("contatti"))
	vat_data = as_mapping(anagrafici.get("id_fiscale_iva"))

	name = normalize_text(anagrafica.get("denominazione"))
	if not name:
		name_parts = [normalize_text(anagrafica.get("nome")), normalize_text(anagrafica.get("cognome"))]
		name = " ".join(part for part in name_parts if part)

	address_parts = [normalize_text(sede.get("indirizzo")), normalize_text(sede.get("numero_civico"))]
	return {
		"display_name": name,
		"supplier_type": "Company" if normalize_text(anagrafica.get("denominazione")) else "Individual",
		"tax_id": compose_tax_id(vat_data),
		"vat_id": normalize_text(vat_data.get("id_codice")),
		"fiscal_code": normalize_text(anagrafici.get("codice_fiscale")),
		"country_code": normalize_text(sede.get("nazione")),
		"country": resolve_country_name(sede.get("nazione")),
		"address_line1": " ".join(part for part in address_parts if part),
		"pincode": normalize_text(sede.get("cap")),
		"city": normalize_text(sede.get("comune")),
		"province": normalize_text(sede.get("provincia")),
		"email": normalize_text(contacts.get("email")),
		"phone": normalize_text(contacts.get("telefono")) or normalize_text(contacts.get("fax")),
		"recipient_pec": normalize_text(transmission.get("pec_destinatario")),
	}


def build_item_preview(line: Mapping[str, Any]) -> dict[str, Any]:
	reference_texts = [
		normalize_text(get_path(extra, "riferimento_testo"))
		for extra in ensure_list(line.get("altri_dati_gestionali"))
		if isinstance(extra, Mapping) and normalize_text(get_path(extra, "riferimento_testo"))
	]
	xml_description = normalize_text(line.get("descrizione"))
	description = first(reference_texts) or xml_description or _("Imported line")
	item_name = extract_item_name(line, fallback_description=xml_description or description)
	notes = []
	if xml_description and xml_description != description:
		notes.append(xml_description)
	notes.extend(reference for reference in reference_texts[1:] if reference != description)
	return {
		"line_no": normalize_text(line.get("numero_linea")),
		"item_name": item_name,
		"description": description,
		"qty": flt(line.get("quantita")) or 1.0,
		"uom": normalize_text(line.get("unita_misura")),
		"rate": flt(line.get("prezzo_unitario")),
		"amount": flt(line.get("prezzo_totale")),
		"tax_rate": flt(line.get("aliquota_iva")),
		"nature": normalize_text(line.get("natura")),
		"admin_reference": normalize_text(line.get("riferimento_amministrazione")),
		"notes": notes,
	}


def extract_item_name(line: Mapping[str, Any], *, fallback_description: str) -> str:
	for code in ensure_list(line.get("codice_articolo")):
		if not isinstance(code, Mapping):
			continue
		code_type = normalize_text(code.get("codice_tipo"))
		if code_type:
			return code_type[:140]
	return collapse_whitespace(fallback_description).split("\n", 1)[0][:140]


def build_tax_preview(row: Mapping[str, Any]) -> dict[str, Any]:
	rate = flt(row.get("aliquota_iva"))
	nature = normalize_text(row.get("natura"))
	reference = normalize_text(row.get("riferimento_normativo"))
	description = reference or (f"VAT {rate:.2f}%" if rate else _("Tax exempt"))
	if nature:
		description = f"{description} ({nature})"
	return {
		"description": description,
		"taxable_amount": flt(row.get("imponibile_importo")),
		"tax_amount": flt(row.get("imposta")),
		"tax_rate": rate,
		"nature": nature,
	}


def build_payment_preview(row: Mapping[str, Any]) -> dict[str, Any]:
	return {
		"mode": normalize_text(row.get("modalita_pagamento")),
		"due_date": normalize_date(row.get("data_scadenza_pagamento")),
		"payment_amount": flt(row.get("importo_pagamento")),
		"iban": normalize_text(row.get("iban")),
		"bank_name": normalize_text(row.get("istituto_finanziario")),
	}


def build_attachment_preview(row: Mapping[str, Any]) -> dict[str, Any]:
	return {
		"name": normalize_text(row.get("nome_attachment")),
		"format": normalize_text(row.get("formato_attachment")),
		"description": normalize_text(row.get("descrizione_attachment")),
	}


def build_supplier_route_options(supplier: Mapping[str, Any]) -> dict[str, Any]:
	return {
		"supplier_name": supplier.get("display_name"),
		"supplier_type": supplier.get("supplier_type"),
		"tax_id": supplier.get("tax_id"),
		"country": supplier.get("country"),
	}


def materialize_inbound_supplier_links(doc, method=None) -> None:
	supplier_preview, payments = load_inbound_supplier_staging(doc)
	if not supplier_preview and not payments:
		return

	ensure_inbound_supplier_contact(doc, supplier_preview)
	ensure_inbound_supplier_address(doc, supplier_preview)
	ensure_inbound_supplier_bank_account(doc, payments)
	clear_inbound_supplier_staging(doc)


def load_inbound_supplier_staging(doc) -> tuple[dict[str, Any], list[dict[str, Any]]]:
	supplier_preview = coerce_json_mapping(doc.get(INBOUND_SUPPLIER_PREVIEW_FIELD))
	payments = [
		as_mapping(row)
		for row in coerce_json_list(doc.get(INBOUND_SUPPLIER_PAYMENTS_FIELD))
		if isinstance(row, Mapping)
	]
	return supplier_preview, payments


def ensure_inbound_supplier_contact(doc, supplier_preview: Mapping[str, Any]) -> None:
	contact_name = normalize_text(doc.get("supplier_primary_contact")) or find_linked_party_record(
		"Contact", "Supplier", doc.name
	)
	if contact_name:
		if doc.supplier_primary_contact != contact_name:
			doc.db_set("supplier_primary_contact", contact_name)
			doc.supplier_primary_contact = contact_name
		return

	email = normalize_text(doc.get("email_id")) or normalize_text(supplier_preview.get("email")) or normalize_text(
		supplier_preview.get("recipient_pec")
	)
	mobile = normalize_text(doc.get("mobile_no")) or normalize_text(supplier_preview.get("phone"))
	if not email and not mobile:
		return

	from erpnext.selling.doctype.customer.customer import make_contact

	contact = make_contact(
		frappe._dict(
			{
				"doctype": "Supplier",
				"name": doc.name,
				"supplier_type": doc.supplier_type,
				"supplier_name": doc.supplier_name,
				"email_id": email,
				"mobile_no": mobile,
			}
		)
	)
	doc.db_set("supplier_primary_contact", contact.name)
	doc.supplier_primary_contact = contact.name
	if email:
		doc.db_set("email_id", email)
		doc.email_id = email
	if mobile:
		doc.db_set("mobile_no", mobile)
		doc.mobile_no = mobile


def ensure_inbound_supplier_address(doc, supplier_preview: Mapping[str, Any]) -> None:
	address_name = normalize_text(doc.get("supplier_primary_address")) or find_linked_party_record(
		"Address", "Supplier", doc.name
	)
	if address_name:
		set_supplier_primary_address_fields(doc, address_name)
		return

	address_line1 = normalize_text(supplier_preview.get("address_line1"))
	city = normalize_text(supplier_preview.get("city"))
	country = normalize_text(doc.get("country")) or normalize_text(supplier_preview.get("country"))
	if not address_line1 or not city or not country:
		return

	from erpnext.selling.doctype.customer.customer import make_address

	address = make_address(
		frappe._dict(
			{
				"doctype": "Supplier",
				"name": doc.name,
				"supplier_name": doc.supplier_name,
				"address_line1": address_line1,
				"address_line2": normalize_text(supplier_preview.get("address_line2")),
				"city": city,
				"state": normalize_text(supplier_preview.get("province")),
				"pincode": normalize_text(supplier_preview.get("pincode")),
				"country": country,
			}
		),
		is_primary_address=1,
		is_shipping_address=0,
	)
	if country and not normalize_text(doc.get("country")):
		doc.db_set("country", country)
		doc.country = country
	set_supplier_primary_address_fields(doc, address.name)


def set_supplier_primary_address_fields(doc, address_name: str) -> None:
	from frappe.contacts.doctype.address.address import get_address_display

	address_display = get_address_display(address_name)
	doc.db_set("supplier_primary_address", address_name)
	doc.supplier_primary_address = address_name
	doc.db_set("primary_address", address_display)
	doc.primary_address = address_display


def ensure_inbound_supplier_bank_account(doc, payments: list[dict[str, Any]]) -> None:
	payment = next((row for row in payments if normalize_text(row.get("iban"))), None)
	if not payment:
		return

	iban_details = parse_iban_details(payment.get("iban"))
	iban = iban_details["iban"]
	if not iban:
		return

	default_bank_account = normalize_text(doc.get("default_bank_account"))
	if default_bank_account:
		normalize_existing_bank_account(default_bank_account, iban_details)
		return

	existing_account = frappe.db.get_value(
		"Bank Account",
		{"party_type": "Supplier", "party": doc.name, "iban": iban},
		"name",
	)
	if existing_account:
		normalize_existing_bank_account(existing_account, iban_details)
		doc.db_set("default_bank_account", existing_account)
		doc.default_bank_account = existing_account
		return

	bank_name = (
		normalize_text(payment.get("bank_name"))
		or resolve_bank_name_from_abi(iban_details.get("abi_code"))
		or IMPORTED_EDI_BANK_NAME
	)
	bank = ensure_bank_record(bank_name)
	account = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": build_bank_account_name(doc.supplier_name or doc.name, iban),
			"bank": bank,
			"party_type": "Supplier",
			"party": doc.name,
			"iban": iban,
			"bank_account_no": iban_details["bank_account_no"],
			"branch_code": iban_details["branch_code"],
			"is_default": 1,
		}
	)
	account.insert(ignore_permissions=True)
	doc.db_set("default_bank_account", account.name)
	doc.default_bank_account = account.name


def ensure_bank_record(bank_name: str) -> str:
	existing_bank = frappe.db.get_value("Bank", {"bank_name": bank_name}, "name")
	if existing_bank:
		return existing_bank

	return frappe.get_doc({"doctype": "Bank", "bank_name": bank_name}).insert(ignore_permissions=True).name


def build_bank_account_name(supplier_name: str, iban: str) -> str:
	suffix = iban[-4:] if len(iban) >= 4 else iban
	return collapse_whitespace(f"{supplier_name} {suffix}")


def parse_iban_details(iban: Any) -> dict[str, str | None]:
	normalized_iban = normalize_text(iban)
	if not normalized_iban:
		return {"iban": None, "abi_code": None, "branch_code": None, "bank_account_no": None}

	normalized_iban = normalized_iban.replace(" ", "").upper()
	if not is_valid_iban(normalized_iban):
		return {"iban": normalized_iban, "abi_code": None, "branch_code": None, "bank_account_no": None}

	details = {"iban": normalized_iban, "abi_code": None, "branch_code": None, "bank_account_no": None}
	if normalized_iban.startswith("IT") and len(normalized_iban) == 27:
		details["abi_code"] = normalized_iban[5:10]
		details["branch_code"] = normalized_iban[10:15]
		details["bank_account_no"] = normalized_iban[15:]

	return details


def normalize_existing_bank_account(bank_account_name: str, iban_details: Mapping[str, str | None]) -> None:
	resolved_bank = get_bank_record_from_abi(iban_details.get("abi_code"))
	updates = {
		key: value
		for key, value in {
			"branch_code": iban_details.get("branch_code"),
			"bank_account_no": iban_details.get("bank_account_no"),
			"bank": resolved_bank["name"] if resolved_bank else None,
		}.items()
		if value is not None
	}
	if not updates:
		return

	bank_account = frappe.get_doc("Bank Account", bank_account_name)
	changed = False
	for fieldname, value in updates.items():
		if bank_account.get(fieldname) == value:
			continue
		bank_account.set(fieldname, value)
		changed = True

	if changed:
		bank_account.save(ignore_permissions=True)


def find_linked_party_record(parenttype: str, link_doctype: str, link_name: str) -> str | None:
	return normalize_text(
		frappe.db.get_value(
			"Dynamic Link",
			{
				"parenttype": parenttype,
				"link_doctype": link_doctype,
				"link_name": link_name,
			},
			"parent",
		)
	)


def clear_inbound_supplier_staging(doc) -> None:
	for fieldname in (INBOUND_SUPPLIER_PREVIEW_FIELD, INBOUND_SUPPLIER_PAYMENTS_FIELD):
		if doc.get(fieldname) in (None, ""):
			continue
		doc.set(fieldname, None)
		frappe.db.set_value(doc.doctype, doc.name, fieldname, None, update_modified=False)


def find_matching_suppliers(supplier: Mapping[str, Any]) -> list[dict[str, str]]:
	candidates = []
	seen = set()
	for filters in (
		{"tax_id": normalize_text(supplier.get("tax_id"))},
		{"supplier_name": normalize_text(supplier.get("display_name"))},
	):
		filter_value = next((value for value in filters.values() if value), None)
		if not filter_value:
			continue
		rows = frappe.get_all("Supplier", filters=filters, fields=["name", "supplier_name", "tax_id"])
		for row in rows:
			if row["name"] in seen:
				continue
			seen.add(row["name"])
			candidates.append(row)
	return candidates


def build_purchase_invoice_items(preview: Mapping[str, Any], *, company: str) -> list[dict[str, Any]]:
	default_uom = get_default_uom()
	default_expense_account = get_default_expense_account(company)
	default_cost_center = get_default_cost_center(company)
	mapping_rows = get_inbound_tax_mapping_rows(company)
	# credit notes (TD04) map to a return Purchase Invoice, which ERPNext requires
	# to carry negative quantities and amounts
	sign = -1 if (preview.get("invoice") or {}).get("is_return") else 1
	rows = []
	for item in preview.get("items") or []:
		description_lines = [normalize_text(item.get("description")) or _("Imported line")]
		if normalize_text(item.get("admin_reference")):
			description_lines.append(_("Administrative Reference: {0}").format(item["admin_reference"]))
		description_lines.extend(note for note in item.get("notes") or [] if normalize_text(note))
		uom = ensure_uom(normalize_text(item.get("uom")) or default_uom)
		item_tax_template = get_inbound_item_tax_template_for_item(
			item,
			company=company,
			mapping_rows=mapping_rows,
		)
		rows.append(
			{
				"item_name": normalize_text(item.get("item_name")) or _("Imported line"),
				"description": "\n".join(description_lines),
				# the line total is the source of truth: some lines carry a discount
				# or rounding where prezzo_unitario * quantita != prezzo_totale, and
				# zero-quantity lines must stay at zero, so pin qty to 1 and rate to
				# the line total instead of letting ERPNext recompute qty * rate
				"qty": sign * 1.0,
				"uom": uom,
				"rate": flt(item.get("amount")),
				"amount": sign * flt(item.get("amount")),
				"conversion_factor": 1.0,
				"expense_account": default_expense_account,
				"cost_center": default_cost_center,
				"item_tax_template": item_tax_template,
			}
		)
	return rows


def build_purchase_invoice_taxes(
	preview: Mapping[str, Any],
	*,
	company: str | None = None,
	tax_account: str | None = None,
	allow_unmapped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	resolved_tax_specs, unresolved_tax_buckets = resolve_purchase_invoice_taxes(
		preview,
		company=company,
		tax_account=tax_account,
		allow_unmapped=allow_unmapped,
	)
	return [spec["row"] for spec in resolved_tax_specs], unresolved_tax_buckets


def build_purchase_invoice_remarks(
	preview: Mapping[str, Any], *, unresolved_tax_buckets: list[dict[str, Any]] | None = None
) -> str:
	invoice = preview.get("invoice") or {}
	supplier = preview.get("supplier") or {}
	lines = [
		_("Imported from inbound EDI supplier invoice."),
		_("Supplier: {0}").format(supplier.get("display_name") or _("Unknown Supplier")),
	]
	if invoice.get("bill_no"):
		lines.append(_("Supplier Invoice No: {0}").format(invoice["bill_no"]))
	lines.append(
		_("EDI totals - Net: {0}, Tax: {1}, Gross: {2}").format(
			flt(invoice.get("total_net_amount")),
			flt(invoice.get("total_tax_amount")),
			flt(invoice.get("total_amount")),
		)
	)
	for payment in preview.get("payments") or []:
		payment_bits = [payment.get("mode"), payment.get("iban"), payment.get("bank_name")]
		payment_bits = [bit for bit in payment_bits if bit]
		if payment_bits:
			lines.append(_("Payment: {0}").format(" / ".join(payment_bits)))
	if unresolved_tax_buckets:
		lines.append(
			_("Tax rows were not loaded automatically because inbound tax mappings are missing for: {0}.").format(
				", ".join(bucket["label"] for bucket in unresolved_tax_buckets)
			)
		)
	return "\n".join(lines)


def get_inbound_tax_mapping_status(company: str, taxes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
	mappings = get_inbound_tax_mapping_index(company)
	resolved = []
	unresolved = []
	for tax in [tax for tax in taxes if should_include_tax_bucket(tax)]:
		bucket = build_tax_bucket_descriptor(tax)
		bucket["account_head"] = mappings.get(get_tax_mapping_key(tax))
		if bucket["account_head"]:
			resolved.append(bucket)
		else:
			unresolved.append(bucket)
	return {"resolved": resolved, "unresolved": unresolved}


def resolve_purchase_invoice_taxes(
	preview: Mapping[str, Any],
	*,
	company: str | None = None,
	tax_account: str | None = None,
	allow_unmapped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	taxes = [tax for tax in preview.get("taxes") or [] if should_include_tax_bucket(tax)]
	if not taxes:
		return [], []

	# on a credit note the items carry negative amounts, so the tax must follow the
	# same sign for the document total (and item-wise details) to add up
	if (preview.get("invoice") or {}).get("is_return"):
		taxes = [
			{**tax, "tax_amount": -flt(tax.get("tax_amount")), "taxable_amount": -flt(tax.get("taxable_amount"))}
			for tax in taxes
		]

	forced_account = normalize_text(tax_account)
	if forced_account:
		return [build_resolved_purchase_tax_spec(tax, forced_account) for tax in taxes], []

	mappings = get_inbound_tax_mapping_index(company)
	resolved_tax_specs = []
	unresolved_tax_buckets = []
	for tax in taxes:
		account_head = mappings.get(get_tax_mapping_key(tax))
		if not account_head:
			unresolved_tax_buckets.append(build_tax_bucket_descriptor(tax))
			continue
		resolved_tax_specs.append(build_resolved_purchase_tax_spec(tax, account_head))

	if unresolved_tax_buckets and not allow_unmapped:
		raise ValidationError(
			_("Configure inbound tax mappings in EDI Configuration for: {0}.").format(
				", ".join(bucket["label"] for bucket in unresolved_tax_buckets)
			)
		)

	return resolved_tax_specs, unresolved_tax_buckets


def build_resolved_purchase_tax_spec(tax: Mapping[str, Any], account_head: str) -> dict[str, Any]:
	account_head = ensure_inbound_natura_account_enabled(
		normalize_text(account_head),
		nature=tax.get("nature"),
	)
	return {
		"source_tax": dict(tax),
		"row": {
			"charge_type": "Actual",
			"account_head": account_head,
			"description": tax["description"],
			"rate": flt(tax.get("tax_rate")),
			"tax_amount": flt(tax.get("tax_amount")),
			"included_in_print_rate": 0,
			"dont_recompute_tax": 1,
		},
	}


def get_inbound_tax_mapping_index(company: str | None) -> dict[tuple[float, str], str]:
	company = normalize_text(company)
	if not company or not frappe.db.exists("EDI Configuration", company):
		return {}

	configuration = frappe.get_cached_doc("EDI Configuration", company)
	return {
		get_tax_mapping_key(mapping): mapping.account_head
		for mapping in (configuration.get("inbound_tax_mappings") or [])
		if normalize_text(getattr(mapping, "account_head", None))
	}


def get_inbound_tax_mapping_rows(company: str | None) -> list[dict[str, Any]]:
	company = normalize_text(company)
	if not company or not frappe.db.exists("EDI Configuration", company):
		return []

	configuration = frappe.get_cached_doc("EDI Configuration", company)
	rows = []
	for mapping in configuration.get("inbound_tax_mappings") or []:
		account_head = normalize_text(getattr(mapping, "account_head", None))
		if not account_head:
			continue
		rows.append(
			{
				"tax_rate": flt(getattr(mapping, "tax_rate", None)),
				"nature": (normalize_text(getattr(mapping, "nature", None)) or "").upper() or None,
				"account_head": account_head,
			}
		)
	return rows


def get_tax_mapping_key(tax: Mapping[str, Any]) -> tuple[float, str]:
	return (
		round(flt(getattr(tax, "tax_rate", None) if not isinstance(tax, Mapping) else tax.get("tax_rate")), 6),
		(
			normalize_text(
				getattr(tax, "nature", None) if not isinstance(tax, Mapping) else tax.get("nature")
			)
			or ""
		).upper(),
	)


def build_tax_bucket_descriptor(tax: Mapping[str, Any]) -> dict[str, Any]:
	rate = flt(tax.get("tax_rate"))
	nature = (normalize_text(tax.get("nature")) or "").upper()
	label = f"{rate:g}%"
	if nature:
		label = f"{label} / {nature}"
	return {
		"tax_rate": rate,
		"nature": nature or None,
		"description": normalize_text(tax.get("description")),
		"label": label,
	}


def should_include_tax_bucket(tax: Mapping[str, Any]) -> bool:
	return (
		abs(flt(tax.get("tax_amount"))) > 0.0001
		or abs(flt(tax.get("tax_rate"))) > 0.0001
		or bool(normalize_text(tax.get("nature")))
	)


def get_inbound_item_tax_template_for_item(
	item: Mapping[str, Any], *, company: str, mapping_rows: list[dict[str, Any]]
) -> str | None:
	selected_mapping = next(
		(
			row
			for row in mapping_rows
			if row["tax_rate"] == round(flt(item.get("tax_rate")), 6)
			and (row.get("nature") or "") == ((normalize_text(item.get("nature")) or "").upper())
		),
		None,
	)
	if not selected_mapping:
		return None

	return ensure_inbound_item_tax_template(
		company=company,
		selected_mapping=selected_mapping,
		mapping_rows=mapping_rows,
	)


def ensure_inbound_item_tax_template(
	*, company: str, selected_mapping: dict[str, Any], mapping_rows: list[dict[str, Any]]
) -> str:
	title = build_inbound_item_tax_template_title(selected_mapping)
	existing = frappe.db.get_value("Item Tax Template", {"company": company, "title": title}, "name")
	taxes = []
	selected_key = (selected_mapping["tax_rate"], selected_mapping.get("nature"))
	for mapping in mapping_rows:
		mapping_key = (mapping["tax_rate"], mapping.get("nature"))
		taxes.append(
			{
				"tax_type": mapping["account_head"],
				"tax_rate": mapping["tax_rate"] if mapping_key == selected_key else 0,
				"not_applicable": 0 if mapping_key == selected_key else 1,
			}
		)

	if existing:
		template = frappe.get_doc("Item Tax Template", existing)
		if build_item_tax_template_signature(template.get("taxes") or []) != build_item_tax_template_signature(taxes):
			template.set("taxes", taxes)
			template.save(ignore_permissions=True)
		return template.name

	template = frappe.get_doc(
		{
			"doctype": "Item Tax Template",
			"title": title,
			"company": company,
			"taxes": taxes,
		}
	)
	template.insert(ignore_permissions=True)
	return template.name


def prepare_purchase_invoice_exact_taxes(
	doc,
	preview: Mapping[str, Any],
	*,
	company: str,
	tax_account: str | None,
) -> None:
	resolved_tax_specs, _ = resolve_purchase_invoice_taxes(
		preview,
		company=company,
		tax_account=tax_account,
		allow_unmapped=True,
	)
	doc.set("item_wise_tax_details", [])
	doc._item_wise_tax_details = []
	doc.update_item_wise_tax_details = True
	if not resolved_tax_specs:
		return

	item_wise_details = build_inbound_item_wise_tax_details(preview, resolved_tax_specs)
	for detail in item_wise_details:
		item_index = detail["item_index"]
		tax_index = detail["tax_index"]
		if item_index >= len(doc.items) or tax_index >= len(doc.taxes):
			continue
		doc._item_wise_tax_details.append(
			frappe._dict(
				item=doc.items[item_index],
				tax=doc.taxes[tax_index],
				rate=detail["rate"],
				amount=detail["amount"],
				taxable_amount=detail["taxable_amount"],
			)
		)


def build_inbound_item_wise_tax_details(
	preview: Mapping[str, Any], resolved_tax_specs: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
	items = list(preview.get("items") or [])
	tax_details = []
	for tax_index, spec in enumerate(resolved_tax_specs):
		source_tax = spec.get("source_tax") or {}
		bucket_key = get_tax_mapping_key(source_tax)
		matching_item_indexes = [
			item_index for item_index, item in enumerate(items) if get_tax_mapping_key(item) == bucket_key
		]
		if not matching_item_indexes:
			continue

		weights = [flt(items[item_index].get("amount")) for item_index in matching_item_indexes]
		allocated_tax_amounts = distribute_amount_by_weights(
			flt(source_tax.get("tax_amount")),
			weights,
		)
		allocated_taxable_amounts = distribute_amount_by_weights(
			flt(source_tax.get("taxable_amount")),
			weights,
		)
		for position, item_index in enumerate(matching_item_indexes):
			tax_details.append(
				{
					"item_index": item_index,
					"tax_index": tax_index,
					"rate": flt(source_tax.get("tax_rate")),
					"amount": allocated_tax_amounts[position],
					"taxable_amount": allocated_taxable_amounts[position],
				}
			)
	return tax_details


def distribute_amount_by_weights(total: float, weights: list[float], *, precision: int = 2) -> list[float]:
	if not weights:
		return []

	total = round(flt(total), precision)
	normalized_weights = [abs(flt(weight)) for weight in weights]
	total_weight = sum(normalized_weights)
	if total_weight <= 0:
		values = [0.0] * len(weights)
		values[-1] = total
		return values

	values = []
	allocated_total = 0.0
	for index, weight in enumerate(normalized_weights):
		if index == len(normalized_weights) - 1:
			value = round(total - allocated_total, precision)
		else:
			value = round(total * weight / total_weight, precision)
			allocated_total = round(allocated_total + value, precision)
		values.append(value)
	return values


def build_inbound_item_tax_template_title(mapping: dict[str, Any]) -> str:
	label = f"Inbound {flt(mapping['tax_rate']):.2f}%"
	if mapping.get("nature"):
		label = f"{label} {mapping['nature']}"
	return label


def build_item_tax_template_signature(rows: list[Mapping[str, Any]]) -> list[tuple[str | None, float, int]]:
	return sorted(
		(
			normalize_text(row.get("tax_type")),
			flt(row.get("tax_rate")),
			1 if row.get("not_applicable") else 0,
		)
		for row in rows or []
	)


def build_purchase_tax_signature(
	rows: list[Mapping[str, Any]],
) -> list[tuple[str | None, str | None, str | None, float, float, int]]:
	return sorted(
		(
			normalize_text(row.get("charge_type")),
			normalize_text(row.get("account_head")),
			normalize_text(row.get("description")),
			flt(row.get("rate")),
			flt(row.get("tax_amount")),
			1 if row.get("dont_recompute_tax") else 0,
		)
		for row in rows or []
	)


def get_default_uom() -> str:
	default_uom = frappe.db.get_single_value("Stock Settings", "stock_uom")
	if not default_uom:
		raise ValidationError(_("Set the default Stock UOM before importing supplier invoices."))
	return default_uom


def get_default_expense_account(company: str) -> str:
	account = frappe.get_cached_value("Company", company, "default_expense_account")
	if account:
		return account

	account = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "root_type": "Expense"},
		"name",
		order_by="lft asc",
	)
	if account:
		return account

	raise ValidationError(_("Set Default Expense Account on Company {0} before importing inbound invoices.").format(company))


def get_default_cost_center(company: str) -> str | None:
	return normalize_text(frappe.get_cached_value("Company", company, "cost_center"))


def ensure_uom(uom_name: str) -> str:
	existing_uom = frappe.db.get_value("UOM", {"uom_name": uom_name}, "name")
	if existing_uom:
		return existing_uom
	new_uom = frappe.new_doc("UOM")
	new_uom.uom_name = uom_name
	new_uom.save()
	return new_uom.name


def read_site_file_with_fallbacks(file_url: str) -> str:
	path = Path(frappe.get_site_path(file_url.lstrip("/")))
	content = path.read_bytes()
	for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
		try:
			return content.decode(encoding)
		except UnicodeDecodeError:
			continue
	raise ValidationError(_("Unable to decode inbound supplier invoice file {0}.").format(file_url))


def element_to_mapping(element: ET.Element) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for child in list(element):
		key = normalize_key(child.tag)
		if key == "attachment":
			value: Any = None
		elif list(child):
			value = element_to_mapping(child)
		else:
			value = normalize_text(child.text)
		if key in result:
			existing = result[key]
			if not isinstance(existing, list):
				result[key] = [existing]
			result[key].append(value)
		else:
			result[key] = value
	return result


def normalize_structure(value: Any) -> Any:
	if isinstance(value, Mapping):
		return {normalize_key(str(key)): normalize_structure(item) for key, item in value.items()}
	if isinstance(value, list):
		return [normalize_structure(item) for item in value]
	return value


def normalize_key(key: str) -> str:
	key = key.split("}", 1)[-1].split(":", 1)[-1]
	key = _CAMEL_BOUNDARY_RE_1.sub(r"\1_\2", key)
	key = _CAMEL_BOUNDARY_RE_2.sub(r"\1_\2", key)
	return key.replace("-", "_").lower()


def get_path(source: Mapping[str, Any] | None, *keys: str) -> Any:
	current: Any = source
	for key in keys:
		current = as_mapping(current).get(key)
		if current is None:
			return None
	return current


def as_mapping(value: Any) -> dict[str, Any]:
	return dict(value) if isinstance(value, Mapping) else {}


def ensure_list(value: Any) -> list[Any]:
	if value is None:
		return []
	if isinstance(value, list):
		return value
	return [value]


def coerce_json_mapping(value: Any) -> dict[str, Any]:
	if isinstance(value, Mapping):
		return dict(value)
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return {}
		try:
			value = json.loads(value)
		except ValueError:
			return {}
	return as_mapping(value)


def coerce_json_list(value: Any) -> list[Any]:
	if isinstance(value, list):
		return value
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return []
		try:
			value = json.loads(value)
		except ValueError:
			return []
	return value if isinstance(value, list) else []


def first(value: Any) -> Any:
	if isinstance(value, list):
		return value[0] if value else None
	return value


def normalize_text(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def normalize_date(value: Any) -> str | None:
	text = normalize_text(value)
	return str(getdate(text)) if text else None


def collapse_whitespace(value: str) -> str:
	return re.sub(r"\s+", " ", value or "").strip()


def compose_tax_id(vat_data: Mapping[str, Any]) -> str | None:
	country = normalize_text(vat_data.get("id_paese"))
	code = normalize_text(vat_data.get("id_codice"))
	if country and code:
		return f"{country}{code}"
	return code


def resolve_country_name(code: Any) -> str | None:
	code = normalize_text(code)
	if not code:
		return None
	return frappe.db.get_value("Country", {"code": code}, "name") or code
