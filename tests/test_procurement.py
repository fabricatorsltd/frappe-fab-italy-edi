from __future__ import annotations

import unittest
from datetime import date
from xml.etree import ElementTree

import frappe
from frappe.exceptions import ValidationError

from fab_italy_edi.fatturapa import procurement


RENDERED_INVOICE = """<?xml version='1.0' encoding='UTF-8'?>
<p:FatturaElettronica
  xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2" versione="FPA12">
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
      </DatiGeneraliDocumento>
      <DatiTrasporto/>
    </DatiGenerali>
  </FatturaElettronicaBody>
</p:FatturaElettronica>
"""


def public_administration_invoice(**overrides):
	fields = {
		"po_no": "Det. 222/2025",
		"po_date": date(2025, 11, 3),
		"fab_edi_cig": "B8DEFEDC66",
		"fab_edi_cup": "",
	}
	fields.update(overrides)
	return frappe._dict(fields)


class TestBuildProcurementReference(unittest.TestCase):
	def test_children_follow_the_schema_order(self):
		reference = procurement.build_procurement_reference(
			public_administration_invoice(fab_edi_cup="J51B25000010004")
		)
		self.assertEqual(
			reference,
			[
				("IdDocumento", "Det. 222/2025"),
				("Data", "2025-11-03"),
				("CodiceCUP", "J51B25000010004"),
				("CodiceCIG", "B8DEFEDC66"),
			],
		)

	def test_missing_date_drops_only_that_child(self):
		reference = procurement.build_procurement_reference(public_administration_invoice(po_date=None))
		self.assertEqual(reference, [("IdDocumento", "Det. 222/2025"), ("CodiceCIG", "B8DEFEDC66")])

	def test_code_without_a_referenced_document_emits_nothing(self):
		self.assertIsNone(procurement.build_procurement_reference(public_administration_invoice(po_no=" ")))

	def test_customer_order_without_a_code_emits_nothing(self):
		# an ordinary invoice carrying a purchase order number must keep rendering as it did
		self.assertIsNone(
			procurement.build_procurement_reference(
				public_administration_invoice(fab_edi_cig="", fab_edi_cup="")
			)
		)


class TestInsertProcurementBlock(unittest.TestCase):
	def patched(self, invoice=None, invoice_xml=RENDERED_INVOICE):
		reference = procurement.build_procurement_reference(invoice or public_administration_invoice())
		return procurement.insert_procurement_block(invoice_xml, reference)

	def test_block_lands_between_the_two_elements_the_schema_puts_it_between(self):
		root = ElementTree.fromstring(self.patched())
		children = [child.tag for child in root.find("FatturaElettronicaBody/DatiGenerali")]
		self.assertEqual(children, ["DatiGeneraliDocumento", "DatiOrdineAcquisto", "DatiTrasporto"])

	def test_block_carries_the_codes(self):
		root = ElementTree.fromstring(self.patched())
		block = root.find("FatturaElettronicaBody/DatiGenerali/DatiOrdineAcquisto")
		self.assertEqual([child.tag for child in block], ["IdDocumento", "Data", "CodiceCIG"])
		self.assertEqual(block.findtext("CodiceCIG"), "B8DEFEDC66")

	def test_markup_in_a_value_is_escaped(self):
		patched = self.patched(public_administration_invoice(po_no="Det. 222 <A&B>"))
		self.assertIn("<IdDocumento>Det. 222 &lt;A&amp;B&gt;</IdDocumento>", patched)
		root = ElementTree.fromstring(patched)
		self.assertEqual(
			root.findtext("FatturaElettronicaBody/DatiGenerali/DatiOrdineAcquisto/IdDocumento"),
			"Det. 222 <A&B>",
		)

	def test_the_block_erpnext_rendered_for_the_same_document_is_replaced_not_repeated(self):
		# ERPNext fills DatiOrdineAcquisto from the item level Customer PO fields, which fetch
		# from the same sales order the header po_no comes from
		invoice_xml = RENDERED_INVOICE.replace(
			"      <DatiTrasporto/>",
			"        <DatiOrdineAcquisto>\n"
			"            <IdDocumento>Det. 222/2025</IdDocumento>\n"
			"            <Data>2025-11-03</Data>\n"
			"        </DatiOrdineAcquisto>\n"
			"      <DatiTrasporto/>",
		)
		patched = self.patched(invoice_xml=invoice_xml)
		root = ElementTree.fromstring(patched)
		blocks = root.findall("FatturaElettronicaBody/DatiGenerali/DatiOrdineAcquisto")
		self.assertEqual(len(blocks), 1)
		self.assertEqual(blocks[0].findtext("CodiceCIG"), "B8DEFEDC66")
		self.assertEqual(
			[child.tag for child in root.find("FatturaElettronicaBody/DatiGenerali")],
			["DatiGeneraliDocumento", "DatiOrdineAcquisto", "DatiTrasporto"],
		)

	def test_a_block_for_another_document_is_kept_alongside_ours(self):
		invoice_xml = RENDERED_INVOICE.replace(
			"      <DatiTrasporto/>",
			"        <DatiOrdineAcquisto>\n"
			"            <IdDocumento>PO-42</IdDocumento>\n"
			"        </DatiOrdineAcquisto>\n"
			"      <DatiTrasporto/>",
		)
		root = ElementTree.fromstring(self.patched(invoice_xml=invoice_xml))
		blocks = root.findall("FatturaElettronicaBody/DatiGenerali/DatiOrdineAcquisto")
		self.assertEqual([block.findtext("IdDocumento") for block in blocks], ["Det. 222/2025", "PO-42"])

	def test_an_xml_already_carrying_a_code_is_left_alone(self):
		invoice_xml = RENDERED_INVOICE.replace(
			"<DatiTrasporto/>", "<DatiOrdineAcquisto><CodiceCIG>B8DEFEDC66</CodiceCIG></DatiOrdineAcquisto>"
		)
		self.assertEqual(self.patched(invoice_xml=invoice_xml), invoice_xml)

	def test_xml_without_the_anchor_is_refused(self):
		with self.assertRaises(ValidationError):
			self.patched(invoice_xml="<p:FatturaElettronica/>")
