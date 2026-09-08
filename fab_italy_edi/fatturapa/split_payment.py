"""ImportoPagamento under split payment, the amount a public administration actually transfers.

Under article 17-ter DPR 633/72 the customer keeps the VAT back and pays it straight to the
treasury, so DatiPagamento has to state what is left while ImportoTotaleDocumento keeps stating the
gross. ERPNext renders DatiPagamento out of the Sales Invoice payment schedule, and that schedule
has to keep carrying the gross, because it is what the receivable is booked at and what the
collection is reconciled against. The amount is therefore restated on the rendered file, on the
text values alone, the way the CIG is written in ``procurement``.

The summary and the payment rows are read over the whole file, which holds because ERPNext renders
one FatturaElettronicaBody per file and never the lotto of several the schema also allows.

Nothing here reads the Sales Invoice: split payment, the VAT and the amounts are all in the file
ERPNext just rendered, and reading them back is what keeps this in step with whatever the core
template decides to put in the payment rows.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# EsigibilitaIVA S is split payment, as opposed to I, immediate, and D, deferred.
SPLIT_PAYMENT_COLLECTABILITY = "S"

SUMMARY_PATTERN = re.compile(r"<DatiRiepilogo>.*?</DatiRiepilogo>", re.DOTALL)
VAT_COLLECTABILITY_PATTERN = re.compile(r"<EsigibilitaIVA>(.*?)</EsigibilitaIVA>", re.DOTALL)
TAX_AMOUNT_PATTERN = re.compile(r"<Imposta>(.*?)</Imposta>", re.DOTALL)
PAYMENT_AMOUNT_PATTERN = re.compile(r"(<ImportoPagamento>)(.*?)(</ImportoPagamento>)", re.DOTALL)

# Amount2DecimalType, the type FatturaPA gives ImportoPagamento, allows two decimals.
CENT = Decimal("0.01")


def is_split_payment(invoice_xml: str) -> bool:
	"""Read the collectability off the rendered file rather than off the invoice.

	ERPNext writes EsigibilitaIVA once per DatiRiepilogo out of a single field, so an invoice is
	either wholly under split payment or not under it at all. A file where the two ever disagree
	is not something we issue, and there is no rule saying which part of a payment would be net,
	so anything but a full S is left exactly as it was rendered.
	"""
	summaries = SUMMARY_PATTERN.findall(invoice_xml)
	if not summaries:
		return False

	collectability = [VAT_COLLECTABILITY_PATTERN.search(summary) for summary in summaries]

	return all(match and match.group(1).strip() == SPLIT_PAYMENT_COLLECTABILITY for match in collectability)


def read_amount(text: str) -> Decimal | None:
	try:
		return Decimal(text.strip())
	except InvalidOperation:
		return None


def vat_total(invoice_xml: str) -> Decimal | None:
	"""Add Imposta up over every DatiRiepilogo, or return nothing if one of them will not read."""
	total = Decimal(0)
	for summary in SUMMARY_PATTERN.findall(invoice_xml):
		for text in TAX_AMOUNT_PATTERN.findall(summary):
			amount = read_amount(text)
			if amount is None:
				return None
			total += amount

	return total


def share_out(net: Decimal, gross_amounts: list[Decimal]) -> list[Decimal]:
	"""Spread the net over the payment rows in proportion to the gross each of them carries.

	Every row is written to the cent, the two decimals Amount2DecimalType allows, and the
	remainder the rounding leaves goes on the last row, so the rows still add up to the net.
	"""
	if len(gross_amounts) == 1:
		return [net]

	gross_total = sum(gross_amounts)
	shares = [
		(net * amount / gross_total).quantize(CENT, rounding=ROUND_HALF_UP) for amount in gross_amounts[:-1]
	]

	return [*shares, net - sum(shares)]


def state_net_amount_due(invoice_xml: str) -> str:
	"""Restate ImportoPagamento as the net on a rendered file under split payment.

	What comes off the rows is the VAT the summary carries, not everything outside ImponibileImporto:
	a stamp duty, an allocated advance, a write off and a rounding adjustment all sit in the payment
	rows and in no DatiRiepilogo, and the customer pays every one of them, so they have to ride along
	untouched. Taking Imposta off what was rendered leaves them exactly where they were.

	The file is expected to be one ERPNext has just rendered, which is what all three of our entry
	points hand over. Running this over a file already restated would take the VAT off twice.
	"""
	if not is_split_payment(invoice_xml):
		return invoice_xml

	gross_amounts = [read_amount(match.group(2)) for match in PAYMENT_AMOUNT_PATTERN.finditer(invoice_xml)]
	tax = vat_total(invoice_xml)
	if not gross_amounts or None in gross_amounts or not tax:
		return invoice_xml

	gross_total = sum(gross_amounts)
	if not gross_total:
		return invoice_xml

	shares = iter(share_out((gross_total - tax).quantize(CENT, rounding=ROUND_HALF_UP), gross_amounts))

	return PAYMENT_AMOUNT_PATTERN.sub(
		lambda match: f"{match.group(1)}{next(shares):.2f}{match.group(3)}", invoice_xml
	)
