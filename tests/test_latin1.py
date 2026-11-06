import unittest

import frappe

from fab_italy_edi.fatturapa import rendered_invoice
from fab_italy_edi.fatturapa.latin1 import to_latin1

from tests.test_procurement import RENDERED_INVOICE


class TestLatin1(unittest.TestCase):
	def test_typographic_punctuation_becomes_plain(self):
		self.assertEqual(
			to_latin1("dell\u2019articolo \u201c1\u201d \u2013 commi\u2026 10\u20ac"),
			"dell'articolo \"1\" - commi... 10EUR",
		)

	def test_latin1_text_is_kept(self):
		text = "Perch\u00e9 \u00e8 gi\u00e0 cos\u00ec: 1.000,00 \u00a0\u00b0 \u00e7"
		self.assertEqual(to_latin1(text), text)

	def test_other_letters_lose_their_accent_and_the_rest_becomes_a_question_mark(self):
		self.assertEqual(to_latin1("Pe\u0161ek \u0151 \u4e2d"), "Pesek o ?")

	def test_invisible_characters_are_dropped(self):
		self.assertEqual(to_latin1("a\u200bb\ufeffc"), "abc")

	def test_the_rendered_invoice_reaches_sdi_in_latin1(self):
		xml = RENDERED_INVOICE.replace(
			"<DatiTrasporto/>", "<DatiTrasporto/>\n<Descrizione>Spesa dell\u2019art. 1</Descrizione>", 1
		)
		self.assertIn("\u2019", xml)
		invoice = frappe._dict(doctype="Sales Invoice", name="FATT/2026/00042")
		patched = rendered_invoice.patch_rendered_invoice(xml, invoice)
		self.assertIn(" dell'art. 1</Descrizione>", patched)
		self.assertTrue(all(ord(char) <= 0xFF for char in patched))


if __name__ == "__main__":
	unittest.main()
