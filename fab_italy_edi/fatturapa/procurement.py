"""DatiOrdineAcquisto, the block that carries the CIG a public administration pays against.

ERPNext renders ``erpnext/regional/italy/e-invoice.xml``, which builds DatiOrdineAcquisto out of
the Sales Invoice Item Customer PO fields and writes only IdDocumento and Data into it: there is
no CodiceCIG and no CodiceCUP anywhere in that template. The codes are written into the rendered
attachment instead of into a template of our own, because a copy of that template would silently
rot on every ERPNext release, while the insertion point here is fixed by the FatturaPA schema,
where DatiOrdineAcquisto is the first element allowed after DatiGeneraliDocumento.

``rendered_invoice`` is what writes the block into the attachment, along with every other
correction we make to the rendered file.
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cstr, getdate

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
