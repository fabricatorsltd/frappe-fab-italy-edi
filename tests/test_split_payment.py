from __future__ import annotations

import unittest
from xml.etree import ElementTree

from fab_italy_edi.fatturapa import split_payment


def rendered_invoice(summaries: str, payments: str) -> str:
	return f"""<?xml version='1.0' encoding='UTF-8'?>
<p:FatturaElettronica
  xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2" versione="FPA12">
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
        <ImportoTotaleDocumento>3013.40</ImportoTotaleDocumento>
      </DatiGeneraliDocumento>
    </DatiGenerali>
    <DatiBeniServizi>
{summaries}    </DatiBeniServizi>
    <DatiPagamento>
      <CondizioniPagamento>TP02</CondizioniPagamento>
{payments}    </DatiPagamento>
  </FatturaElettronicaBody>
</p:FatturaElettronica>
"""


def summary(taxable: str, tax: str, collectability: str | None = "S") -> str:
	collectability_line = (
		f"          <EsigibilitaIVA>{collectability}</EsigibilitaIVA>\n" if collectability else ""
	)
	return (
		"      <DatiRiepilogo>\n"
		"        <AliquotaIVA>22.00</AliquotaIVA>\n"
		f"        <ImponibileImporto>{taxable}</ImponibileImporto>\n"
		f"        <Imposta>{tax}</Imposta>\n"
		f"{collectability_line}"
		"      </DatiRiepilogo>\n"
	)


def payment(amount: str, due_date: str = "2026-10-07") -> str:
	return (
		"      <DettaglioPagamento>\n"
		"        <ModalitaPagamento>MP05</ModalitaPagamento>\n"
		f"        <DataScadenzaPagamento>{due_date}</DataScadenzaPagamento>\n"
		f"        <ImportoPagamento>{amount}</ImportoPagamento>\n"
		"          <IBAN>IT30S0326811200052945656640</IBAN>\n"
		"        <CodicePagamento>FATT/2026/00035</CodicePagamento>\n"
		"      </DettaglioPagamento>\n"
	)


# the prod invoice to the Comune di Pompiano: 2470.00 taxable, 543.40 VAT, one payment on the gross
POMPIANO = rendered_invoice(summary("2470.00", "543.40"), payment("3013.40"))

# the same invoice to a customer who pays us the VAT
POMPIANO_ORDINARY_VAT = rendered_invoice(summary("2470.00", "543.40", collectability="I"), payment("3013.40"))


def payment_amounts(invoice_xml: str) -> list[str]:
	root = ElementTree.fromstring(invoice_xml)
	return [
		detail.findtext("ImportoPagamento")
		for detail in root.findall("FatturaElettronicaBody/DatiPagamento/DettaglioPagamento")
	]


class TestStateNetAmountDue(unittest.TestCase):
	def test_the_only_payment_row_states_the_taxable_amount(self):
		patched = split_payment.state_net_amount_due(POMPIANO)
		self.assertEqual(payment_amounts(patched), ["2470.00"])

	def test_nothing_but_the_payment_amount_is_rewritten(self):
		patched = split_payment.state_net_amount_due(POMPIANO)
		self.assertEqual(patched, POMPIANO.replace("<ImportoPagamento>3013.40<", "<ImportoPagamento>2470.00<"))

	def test_the_document_total_keeps_the_gross(self):
		root = ElementTree.fromstring(split_payment.state_net_amount_due(POMPIANO))
		self.assertEqual(
			root.findtext(
				"FatturaElettronicaBody/DatiGenerali/DatiGeneraliDocumento/ImportoTotaleDocumento"
			),
			"3013.40",
		)

	def test_several_rows_share_the_net_and_add_up_to_it(self):
		# 1000.00 net in three instalments of the 1220.00 gross, none of which divides evenly
		invoice_xml = rendered_invoice(
			summary("1000.00", "220.00"),
			payment("406.67") + payment("406.67", "2026-11-07") + payment("406.66", "2026-12-07"),
		)
		amounts = payment_amounts(split_payment.state_net_amount_due(invoice_xml))
		self.assertEqual(amounts, ["333.34", "333.34", "333.32"])
		self.assertEqual(sum(float(amount) for amount in amounts), 1000.00)

	def test_the_vat_of_every_summary_comes_off(self):
		invoice_xml = rendered_invoice(
			summary("1000.00", "220.00") + summary("500.00", "110.00"), payment("1830.00")
		)
		self.assertEqual(payment_amounts(split_payment.state_net_amount_due(invoice_xml)), ["1500.00"])

	def test_an_invoice_outside_split_payment_is_left_alone(self):
		self.assertEqual(split_payment.state_net_amount_due(POMPIANO_ORDINARY_VAT), POMPIANO_ORDINARY_VAT)

	def test_mixed_collectability_is_left_alone(self):
		invoice_xml = rendered_invoice(
			summary("1000.00", "220.00") + summary("500.00", "110.00", collectability="I"),
			payment("1830.00"),
		)
		self.assertEqual(split_payment.state_net_amount_due(invoice_xml), invoice_xml)

	def test_a_summary_without_a_collectability_is_left_alone(self):
		invoice_xml = rendered_invoice(summary("2470.00", "543.40", collectability=None), payment("3013.40"))
		self.assertEqual(split_payment.state_net_amount_due(invoice_xml), invoice_xml)

	def test_an_invoice_without_a_payment_block_is_left_alone(self):
		invoice_xml = rendered_invoice(summary("2470.00", "543.40"), "")
		self.assertEqual(split_payment.state_net_amount_due(invoice_xml), invoice_xml)

	def test_a_stamp_duty_the_summary_does_not_carry_stays_on_the_amount_due(self):
		# the 2.00 stamp duty is booked as a fixed amount, so it lands in the payment row and in no
		# summary: the customer pays it, only the VAT goes to the treasury
		invoice_xml = rendered_invoice(summary("2470.00", "543.40"), payment("3015.40"))
		self.assertEqual(payment_amounts(split_payment.state_net_amount_due(invoice_xml)), ["2472.00"])

	def test_an_invoice_carrying_no_vat_is_left_alone(self):
		invoice_xml = rendered_invoice(summary("2470.00", "0.00"), payment("2470.00"))
		self.assertEqual(split_payment.state_net_amount_due(invoice_xml), invoice_xml)
