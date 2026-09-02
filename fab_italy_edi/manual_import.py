"""Import inbound supplier invoices by hand from FatturaPA files downloaded from
the SdI portal, for the invoices the intermediary never delivered. Each XML
becomes the same EDI Document the provider polling would have created, so the
usual review and Purchase Invoice draft flow applies afterwards."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import frappe
from frappe import _
from frappe.exceptions import ValidationError

from fab_italy_edi.edi_lifecycle import persist_payload_artifact
from fab_italy_edi.purchase_invoice_import import parse_supplier_invoice_source

METADATA_SUFFIX = re.compile(r"_MT_\d+\.xml$", re.IGNORECASE)
DEFAULT_PROVIDER = "OpenAPI SDI Proxy"


def import_incoming_files(directory: str, provider: str = DEFAULT_PROVIDER, company: str | None = None) -> list[dict]:
	"""Create supplier_invoice_import EDI Documents from every FatturaPA XML in
	`directory`. A metadata file next to it (<name>_MT_001.xml) supplies the SdI
	identifier; files already imported are reported and skipped."""
	provider_doc = frappe.get_doc("EDI Provider", provider)
	filters = {"enabled": 1}
	if company:
		filters["company"] = company
	configurations = frappe.get_all("EDI Configuration", filters=filters, fields=["name", "company"], limit=1)
	if not configurations:
		raise ValidationError(_("No enabled EDI Configuration found."))
	configuration = configurations[0]

	results = []
	for filename in sorted(os.listdir(directory)):
		if not filename.lower().endswith(".xml") or METADATA_SUFFIX.search(filename):
			continue
		results.append(import_incoming_file(os.path.join(directory, filename), configuration, provider_doc))
	return results


def import_incoming_file(path: str, configuration, provider_doc) -> dict:
	filename = os.path.basename(path)
	with open(path, encoding="utf-8-sig") as f:
		xml = f.read()
	metadata = read_metadata(path)
	identifier = metadata.get("IdentificativoSdI") or os.path.splitext(filename)[0]

	existing = frappe.db.get_value(
		"EDI Document", {"document_kind": "supplier_invoice_import", "canonical_identifier": filename}, "name"
	) or frappe.db.get_value(
		"EDI Document", {"document_kind": "supplier_invoice_import", "external_submission_id": identifier}, "name"
	)
	if existing:
		return {"file": filename, "edi_document": existing, "created": False}

	preview = parse_supplier_invoice_source(xml)
	supplier = preview.get("supplier") or {}
	document = frappe.get_doc(
		{
			"doctype": "EDI Document",
			"source_doctype": "EDI Configuration",
			"source_name": configuration.name,
			"company": configuration.company,
			"document_kind": "supplier_invoice_import",
			"party_name": supplier.get("display_name"),
			"channel": provider_doc.channel,
			"provider": provider_doc.name,
			"validation_state": "not_validated",
			"transmission_state": "ready",
			"latest_receipt_state": "delivered",
			"canonical_identifier": filename,
			"idempotency_key": f"incoming:{identifier}",
			"external_submission_id": identifier,
		}
	)
	document.insert(ignore_permissions=True)
	document.source_xml = persist_payload_artifact(
		document, f"manual-incoming-{os.path.splitext(filename)[0]}", xml, fieldname="source_xml"
	)
	document.append(
		"receipts",
		{
			"receipt_type": "supplier_invoice_fetched",
			"normalized_state": "delivered",
			"matched": 1,
			"external_message_id": identifier,
			"processing_notes": _("Imported by hand from the SdI portal file {0}").format(filename),
		},
	)
	document.save(ignore_permissions=True)
	return {"file": filename, "edi_document": document.name, "created": True, "supplier": supplier.get("display_name")}


def read_metadata(path: str) -> dict:
	"""IdentificativoSdI and friends from the <name>_MT_001.xml file next to the invoice."""
	stem = os.path.splitext(path)[0]
	for candidate in sorted(os.listdir(os.path.dirname(path) or ".")):
		full = os.path.join(os.path.dirname(path) or ".", candidate)
		if full.startswith(stem + "_MT_") and candidate.lower().endswith(".xml"):
			root = ET.fromstring(open(full, encoding="utf-8-sig").read())
			return {re.sub(r"^\{.*\}", "", child.tag): (child.text or "").strip() for child in root}
	return {}
