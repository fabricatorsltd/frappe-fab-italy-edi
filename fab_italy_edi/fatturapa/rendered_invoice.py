"""The one place the e-invoice ERPNext rendered is corrected before it leaves for SDI.

``erpnext.regional.italy.utils`` renders and attaches the XML on submit, and regenerates it with
``replace=True`` on the Generate E-Invoice button and on the send path. Everything the core
template cannot say is written into that rendered file here, so the attachment the invoice carries
and the file transmitted under the same progressive number never disagree: the CIG block from
``procurement`` and the payment amount from ``split_payment``.

Each correction stands on its own and reads the rendered file for what it needs, so an invoice
under split payment carrying no procurement reference still has its payment amount restated, and a
public administration invoice outside split payment still gets its CIG.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from erpnext.regional.italy.utils import get_e_invoice_attachments

from fab_italy_edi.fatturapa.procurement import build_procurement_reference, insert_procurement_block
from fab_italy_edi.fatturapa.regional_compat import is_italian_company
from fab_italy_edi.fatturapa.split_payment import state_net_amount_due


def patch_rendered_invoice(invoice_xml: str, invoice: Any) -> str:
	"""Run every correction over the rendered XML. They touch different elements, so neither
	depends on the other having run."""
	reference = build_procurement_reference(invoice)
	if reference:
		invoice_xml = insert_procurement_block(invoice_xml, reference)

	return state_net_amount_due(invoice_xml)


def patch_attachment(attachment: Any, invoice: Any) -> Any:
	"""Correct the e-invoice ERPNext just rendered, and return the attachment to use."""
	invoice_xml = cstr(attachment.get_content())
	patched = patch_rendered_invoice(invoice_xml, invoice)
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


def patch_attached_invoice(invoice: Any, method: str | None = None) -> None:
	"""Correct the e-invoice ERPNext attached on submit.

	``erpnext.regional.italy.utils.sales_invoice_on_submit`` renders and attaches the XML from the
	hook registered before this one, and the send path regenerates it; both end up in
	``patch_attachment``, so the attachment and the transmitted file say the same thing.
	"""
	if not is_italian_company(invoice.get("company")):
		return

	attachments = get_e_invoice_attachments(invoice) or []
	if not attachments:
		return

	patch_attachment(frappe.get_doc("File", attachments[0].name), invoice)


@frappe.whitelist()
def generate_single_invoice(docname: str) -> str:
	"""ERPNext's Generate E-Invoice button, with our corrections put back on the file it writes.

	Overridden in ``hooks.py`` because ERPNext regenerates the attachment from the core template
	alone: one click would replace the transmitted file with one carrying no CIG and the gross as
	the amount due, under the same progressive number, and the file downloaded from the button
	would say the same.
	"""
	from erpnext.regional.italy.utils import prepare_and_attach_invoice

	from fab_italy_edi.api import validate_procurement_reference

	invoice = frappe.get_doc("Sales Invoice", docname)
	frappe.has_permission("Sales Invoice", doc=invoice, throw=True)

	validate_procurement_reference(invoice)

	return patch_attachment(prepare_and_attach_invoice(invoice, True), invoice).file_url
