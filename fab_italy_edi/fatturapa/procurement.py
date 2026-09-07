"""DatiOrdineAcquisto, the block that carries the CIG a public administration pays against.

ERPNext renders ``erpnext/regional/italy/e-invoice.xml``, which builds DatiOrdineAcquisto out of
the Sales Invoice Item Customer PO fields and writes only IdDocumento and Data into it: there is
no CodiceCIG and no CodiceCUP anywhere in that template. The codes are written into the rendered
attachment instead of into a template of our own, because a copy of that template would silently
rot on every ERPNext release, while the insertion point here is fixed by the FatturaPA schema,
where DatiOrdineAcquisto is the first element allowed after DatiGeneraliDocumento.
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cstr, getdate

from erpnext.regional.italy.utils import get_e_invoice_attachments

from fab_italy_edi.fatturapa.regional_compat import is_italian_company

# DatiGeneraliDocumento is the only element the schema puts before DatiOrdineAcquisto, and it
# occurs once, so its closing tag is the anchor.
DATI_GENERALI_DOCUMENTO_END = "</DatiGeneraliDocumento>"

ORDER_BLOCK_PATTERN = re.compile(r"<DatiOrdineAcquisto>.*?</DatiOrdineAcquisto>", re.DOTALL)

# Longest value each child of DatiDocumentiCorrelatiType accepts: IdDocumento is String20Type,
# CodiceCUP and CodiceCIG are String15Type. SDI refuses the whole file over the limit.
MAX_DOCUMENT_ID_LENGTH = 20
MAX_PROCUREMENT_CODE_LENGTH = 15


def build_procurement_reference(invoice: Any) -> list[tuple[str, str]] | None:
	"""Read the order the invoice refers to, with the CIG and the CUP that belong to it.

	Returns the children of DatiOrdineAcquisto in the order DatiDocumentiCorrelatiType fixes
	them, which SDI checks: RiferimentoNumeroLinea, IdDocumento, Data, NumItem,
	CodiceCommessaConvenzione, CodiceCUP, CodiceCIG. Nothing is emitted without a referenced
	document, because IdDocumento is the one mandatory child, nor without a code, so an invoice
	carrying only a customer order keeps rendering exactly as ERPNext rendered it before.
	"""
	document_id = cstr(invoice.get("po_no")).strip()
	cig = cstr(invoice.get("fab_edi_cig")).strip()
	cup = cstr(invoice.get("fab_edi_cup")).strip()
	if not document_id or not (cig or cup):
		return None

	document_date = invoice.get("po_date")
	children = (
		("IdDocumento", document_id),
		("Data", getdate(document_date).isoformat() if document_date else ""),
		("CodiceCUP", cup),
		("CodiceCIG", cig),
	)

	return [(tag, value) for tag, value in children if value]


def render_procurement_block(reference: list[tuple[str, str]]) -> str:
	"""Lay the block out the way the ERPNext template lays out its own DatiOrdineAcquisto loop."""
	children = "".join(f"\n            <{tag}>{escape(value)}</{tag}>" for tag, value in reference)

	return f"\n        <DatiOrdineAcquisto>{children}\n        </DatiOrdineAcquisto>"


def insert_procurement_block(invoice_xml: str, reference: list[tuple[str, str]]) -> str:
	"""Write the block into the rendered XML, in the one place the schema allows it.

	An XML already carrying a code is left alone, so the day ERPNext writes the codes itself
	this stops adding a second block. When ERPNext already rendered the same referenced document
	from the item level Customer PO fields, ours takes its place rather than repeating it: the
	template writes only IdDocumento and Data there, which ours carries too.
	"""
	if "<CodiceCIG>" in invoice_xml or "<CodiceCUP>" in invoice_xml:
		return invoice_xml

	block = render_procurement_block(reference)
	rendered = find_rendered_order_block(invoice_xml, reference[0][1])
	if rendered:
		return invoice_xml.replace(rendered, block.lstrip(), 1)

	head, marker, tail = invoice_xml.partition(DATI_GENERALI_DOCUMENTO_END)
	if not marker:
		raise ValidationError(
			_("The generated e-invoice has no DatiGeneraliDocumento, so the CIG has nowhere to go.")
		)

	return head + marker + block + tail


def find_rendered_order_block(invoice_xml: str, document_id: str) -> str | None:
	"""Return the DatiOrdineAcquisto ERPNext already wrote for the same referenced document."""
	wanted = f"<IdDocumento>{escape(document_id)}</IdDocumento>"

	return next(
		(match.group(0) for match in ORDER_BLOCK_PATTERN.finditer(invoice_xml) if wanted in match.group(0)),
		None,
	)


def apply_procurement_reference(attachment: Any, invoice: Any) -> Any:
	"""Put the CIG on the e-invoice ERPNext just rendered, and return the attachment to use."""
	reference = build_procurement_reference(invoice)
	if not reference:
		return attachment

	invoice_xml = cstr(attachment.get_content())
	patched = insert_procurement_block(invoice_xml, reference)
	if patched == invoice_xml:
		return attachment

	return rewrite_attachment(attachment, patched)


def rewrite_attachment(attachment: Any, invoice_xml: str) -> Any:
	"""Write the patched XML over the attachment, keeping the File row it is already on.

	The file name carries the progressive number SDI tracks the transmission by, and the invoice
	timeline should show one attachment, so the blob is overwritten rather than replaced by a
	second File. ``ignore_existing_file_check`` skips the deduplication by content hash, which
	only applies to new files anyway.
	"""
	attachment.save_file(content=invoice_xml, ignore_existing_file_check=True, overwrite=True)
	attachment.save()

	return attachment


def attach_procurement_reference(invoice: Any, method: str | None = None) -> None:
	"""Put the CIG on the e-invoice ERPNext attached on submit.

	``erpnext.regional.italy.utils.sales_invoice_on_submit`` renders and attaches the XML from
	the hook registered before this one, and the send path regenerates it with ``replace=True``;
	both end up in ``apply_procurement_reference``, so the attachment and the transmitted file
	say the same thing.
	"""
	if not is_italian_company(invoice.get("company")):
		return

	if not build_procurement_reference(invoice):
		return

	attachments = get_e_invoice_attachments(invoice) or []
	if not attachments:
		return

	apply_procurement_reference(frappe.get_doc("File", attachments[0].name), invoice)


@frappe.whitelist()
def generate_single_invoice(docname: str) -> str:
	"""ERPNext's Generate E-Invoice button, with the codes put back on the file it writes.

	Overridden in ``hooks.py`` because ERPNext regenerates the attachment from the core template
	alone: one click would replace the transmitted file with one carrying no CIG, under the same
	progressive number, and the file downloaded from the button would say the same.
	"""
	from erpnext.regional.italy.utils import prepare_and_attach_invoice

	from fab_italy_edi.api import validate_procurement_reference

	invoice = frappe.get_doc("Sales Invoice", docname)
	frappe.has_permission("Sales Invoice", doc=invoice, throw=True)

	validate_procurement_reference(invoice)

	return apply_procurement_reference(prepare_and_attach_invoice(invoice, True), invoice).file_url
