from frappe.model.document import Document


class Autofattura(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_autofattura_line.edi_autofattura_line import (
			EDIAutofatturaLine,
		)
		from frappe.types import DF

		company: DF.Link
		currency: DF.Link
		document_date: DF.Date
		document_number: DF.Data | None
		document_type: DF.Select
		generated_xml: DF.Attach | None
		grand_total: DF.Currency
		last_error: DF.SmallText | None
		latest_receipt_state: DF.Select | None
		lines: DF.Table[EDIAutofatturaLine]
		linked_edi_document: DF.Link | None
		naming_series: DF.Data | None
		net_total: DF.Currency
		source_purchase_invoice: DF.Link
		supplier: DF.Link
		supplier_invoice_date: DF.Date
		supplier_invoice_number: DF.Data
		supplier_name: DF.Data | None
		tax_total: DF.Currency
		transmission_state: DF.Select
		validation_state: DF.Select
	# end: auto-generated types

	def validate(self):
		from fab_italy_edi.autofattura import sync_autofattura_totals

		sync_autofattura_totals(self)
