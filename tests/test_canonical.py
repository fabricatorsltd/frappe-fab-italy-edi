from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from fab_italy_edi.fatturapa.canonical import from_purchase_invoice, from_sales_invoice


class TestCanonicalInvoice(unittest.TestCase):
	def test_sales_invoice_mapping_preserves_procurement_references(self):
		invoice = SimpleNamespace(
			name="SINV-0001",
			is_return=0,
			company="Fabricators SRL",
			customer="Customer A",
			customer_name="Customer A SRL",
			tax_id="IT12345678901",
			posting_date=date(2026, 4, 24),
			due_date=date(2026, 5, 24),
			currency="EUR",
			grand_total="123.45",
			remarks="Test invoice",
			po_no="PO-42",
			po_date=date(2026, 4, 1),
			fab_edi_cig="1234567890",
			fab_edi_cup="CUP-001",
		)

		canonical = from_sales_invoice(invoice)

		self.assertEqual(canonical.document_kind, "invoice")
		self.assertEqual(canonical.party.name, "Customer A SRL")
		self.assertEqual(canonical.grand_total, Decimal("123.45"))
		self.assertEqual(len(canonical.procurement_references), 1)
		self.assertEqual(canonical.procurement_references[0].document_id, "PO-42")
		self.assertEqual(canonical.procurement_references[0].cig, "1234567890")
		self.assertEqual(canonical.procurement_references[0].cup, "CUP-001")

	def test_sales_return_maps_to_credit_note(self):
		canonical = from_sales_invoice(
			{
				"name": "SINV-RET-0001",
				"is_return": 1,
				"company": "Fabricators SRL",
				"customer": "Customer A",
			}
		)

		self.assertEqual(canonical.document_kind, "credit_note")
		self.assertEqual(canonical.source_name, "SINV-RET-0001")

	def test_purchase_invoice_mapping_uses_supplier_identity(self):
		canonical = from_purchase_invoice(
			{
				"name": "PINV-0001",
				"company": "Fabricators SRL",
				"supplier": "Supplier A",
				"supplier_name": "Supplier A SRL",
				"tax_id": "IT99887766554",
				"grand_total": "99.99",
			}
		)

		self.assertEqual(canonical.document_kind, "supplier_invoice_import")
		self.assertEqual(canonical.party.name, "Supplier A SRL")
		self.assertEqual(canonical.grand_total, Decimal("99.99"))
