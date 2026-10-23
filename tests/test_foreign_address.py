import unittest

import frappe

from fab_italy_edi.fatturapa import autofattura as autofattura_xml
from fab_italy_edi.fatturapa import rendered_invoice
from fab_italy_edi.fatturapa.foreign_address import get_foreign_pincode, restate_foreign_pincodes


def render_sede(pincode, country_code, province=None):
	province_line = f"\n<Provincia>{province}</Provincia>" if province else ""
	return (
		f"<Sede>\n<Indirizzo>Main St 1</Indirizzo>\n<CAP>{pincode}</CAP>\n<Comune>Belize</Comune>"
		f"{province_line}\n<Nazione>{country_code}</Nazione>\n</Sede>"
	)


class TestForeignPincode(unittest.TestCase):
	def test_a_five_character_postcode_that_is_not_five_digits_becomes_zeros(self):
		self.assertEqual(get_foreign_pincode("N4892"), "00000")

	def test_a_foreign_postcode_of_five_digits_is_kept(self):
		self.assertEqual(get_foreign_pincode(" 10115 "), "10115")

	def test_a_missing_or_short_postcode_becomes_zeros(self):
		for pincode in (None, "", "2000", "SW1A 1AA", "123456"):
			self.assertEqual(get_foreign_pincode(pincode), "00000")

	def test_the_autofattura_seller_address_carries_zeros_for_a_letter_postcode(self):
		address = frappe._dict(
			name="Supplier-Billing", address_line1="1 Main St", pincode="N4892", city="Belize", country_code="BZ"
		)
		self.assertEqual(autofattura_xml.build_xml_address(address, foreign=True)["pincode"], "00000")

	def test_the_autofattura_italian_address_keeps_its_cap(self):
		address = frappe._dict(
			name="Office-Billing", address_line1="Via Roma 1", pincode="25020", city="Pompiano", state_code="BS", country_code="IT"
		)
		self.assertEqual(autofattura_xml.build_xml_address(address, foreign=False)["pincode"], "25020")


class TestRestateForeignPincodes(unittest.TestCase):
	def test_a_foreign_customer_cap_is_restated(self):
		xml = render_sede("N4892", "BZ")
		self.assertEqual(restate_foreign_pincodes(xml), render_sede("00000", "BZ"))

	def test_an_italian_cap_is_left_alone_even_when_wrong(self):
		xml = render_sede("2502", "IT", "BS")
		self.assertEqual(restate_foreign_pincodes(xml), xml)

	def test_each_address_is_read_against_its_own_country(self):
		xml = render_sede("25020", "IT", "BS") + render_sede("SW1A 1AA", "GB")
		self.assertEqual(restate_foreign_pincodes(xml), render_sede("25020", "IT", "BS") + render_sede("00000", "GB"))

	def test_the_rendered_invoice_correction_restates_the_foreign_cap(self):
		xml = "<CessionarioCommittente>" + render_sede("N4892", "BZ") + "</CessionarioCommittente>"
		invoice = frappe._dict(doctype="Sales Invoice", name="FATT/2026/00040")
		self.assertIn("<CAP>00000</CAP>", rendered_invoice.patch_rendered_invoice(xml, invoice))
