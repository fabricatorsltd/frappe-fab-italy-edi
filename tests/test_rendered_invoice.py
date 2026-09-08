from __future__ import annotations

import unittest
from xml.etree import ElementTree

import frappe

from fab_italy_edi.fatturapa import rendered_invoice

from tests.test_procurement import RENDERED_INVOICE, public_administration_invoice
from tests.test_split_payment import POMPIANO, POMPIANO_ORDINARY_VAT


class FakeAttachment:
	def __init__(self, content):
		self.name = "file-0001"
		self.file_name = "IT04266880980_00001.xml"
		self._content = content
		self.written = None
		self.save_calls = 0

	def get_content(self):
		return self._content

	def save_file(self, content=None, ignore_existing_file_check=False, overwrite=False):
		self.written = content
		self.overwrite = overwrite

	def save(self):
		self.save_calls += 1


class TestPatchAttachment(unittest.TestCase):
	def test_an_ordinary_invoice_keeps_the_attachment_erpnext_wrote(self):
		attachment = FakeAttachment(RENDERED_INVOICE)
		invoice = frappe._dict(doctype="Sales Invoice", name="FATT/2026/00034", po_no="PO-42")
		self.assertIs(rendered_invoice.patch_attachment(attachment, invoice), attachment)
		self.assertIsNone(attachment.written)

	def test_an_invoice_with_nothing_to_correct_keeps_the_file_it_was_rendered_as(self):
		attachment = FakeAttachment(POMPIANO_ORDINARY_VAT)
		invoice = frappe._dict(doctype="Sales Invoice", name="FATT/2026/00036", po_no="PO-42")
		self.assertIs(rendered_invoice.patch_attachment(attachment, invoice), attachment)
		self.assertIsNone(attachment.written)

	def test_the_patched_xml_is_written_over_the_same_attachment(self):
		attachment = FakeAttachment(RENDERED_INVOICE)
		result = rendered_invoice.patch_attachment(attachment, public_administration_invoice())
		self.assertIs(result, attachment)
		self.assertEqual(attachment.save_calls, 1)
		self.assertTrue(attachment.overwrite)
		self.assertIn("<CodiceCIG>B8DEFEDC66</CodiceCIG>", attachment.written)
		ElementTree.fromstring(attachment.written)

	def test_split_payment_is_stated_on_an_invoice_carrying_no_procurement_reference(self):
		attachment = FakeAttachment(POMPIANO)
		invoice = frappe._dict(doctype="Sales Invoice", name="FATT/2026/00035", po_no="")
		rendered_invoice.patch_attachment(attachment, invoice)
		self.assertIn("<ImportoPagamento>2470.00</ImportoPagamento>", attachment.written)
		self.assertNotIn("<CodiceCIG>", attachment.written)

	def test_the_cig_and_the_amount_due_are_both_written_on_the_same_file(self):
		attachment = FakeAttachment(POMPIANO)
		rendered_invoice.patch_attachment(attachment, public_administration_invoice())
		self.assertEqual(attachment.save_calls, 1)
		self.assertIn("<CodiceCIG>B8DEFEDC66</CodiceCIG>", attachment.written)
		self.assertIn("<ImportoPagamento>2470.00</ImportoPagamento>", attachment.written)
		root = ElementTree.fromstring(attachment.written)
		self.assertEqual(
			[child.tag for child in root.find("FatturaElettronicaBody/DatiGenerali")],
			["DatiGeneraliDocumento", "DatiOrdineAcquisto"],
		)
