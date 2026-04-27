from frappe.model.document import Document


class EDIDocument(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		canonical_identifier: DF.Data | None
		channel: DF.Link | None
		company: DF.Link
		document_kind: DF.Select
		autofattura_document_type: DF.Select | None
		autofattura_document_date: DF.Date | None
		autofattura_document_number: DF.Data | None
		autofattura_naming_series: DF.Data | None
		autofattura_reference_invoice_number: DF.Data | None
		autofattura_reference_invoice_date: DF.Date | None
		autofattura_currency: DF.Link | None
		autofattura_net_total: DF.Currency
		autofattura_tax_total: DF.Currency
		autofattura_grand_total: DF.Currency
		autofattura_lines: DF.Table[EDIAutofatturaLine]
		external_submission_id: DF.Data | None
		generated_xml: DF.Attach | None
		idempotency_key: DF.Data | None
		latest_receipt_state: DF.Select | None
		last_error: DF.SmallText | None
		party_name: DF.Data | None
		provider: DF.Link | None
		receipts: DF.Table[EDIReceipt]
		source_doctype: DF.Link
		source_name: DF.DynamicLink
		source_xml: DF.Attach | None
		transmission_attempts: DF.Table[EDITransmissionAttempt]
		transmission_state: DF.Select
		validation_state: DF.Select

		from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_receipt.edi_receipt import EDIReceipt
		from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_autofattura_line.edi_autofattura_line import (
			EDIAutofatturaLine,
		)
		from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_transmission_attempt.edi_transmission_attempt import (
			EDITransmissionAttempt,
		)
	# end: auto-generated types

	def validate(self):
		if self.document_kind != "autofattura":
			return

		from fab_italy_edi.autofattura import sync_autofattura_editor_totals

		sync_autofattura_editor_totals(self)
