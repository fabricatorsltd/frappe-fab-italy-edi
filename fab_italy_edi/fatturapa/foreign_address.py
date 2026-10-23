"""CAP on a foreign address, which FatturaPA types as five digits whatever the country.

SDI refuses the whole file over a CAP that is not [0-9]{5}, and a foreign postcode rarely is
(N4892, SW1A 1AA, 2000). The specifications ask for 00000 on a non Italian address instead, so a
foreign postcode is kept only when it already fits.
"""

from __future__ import annotations

import re

from frappe.utils import cstr

FOREIGN_ADDRESS_PINCODE = "00000"

CAP_PATTERN = re.compile(r"\d{5}")
# render_address in the ERPNext template writes CAP, Comune, an optional Provincia and Nazione,
# so the first Nazione after a CAP is the country of that same address.
ADDRESS_PATTERN = re.compile(r"(<CAP>)(.*?)(</CAP>.*?<Nazione>)(.*?)(</Nazione>)", re.DOTALL)


def get_foreign_pincode(pincode) -> str:
	pincode = cstr(pincode).strip()
	return pincode if CAP_PATTERN.fullmatch(pincode) else FOREIGN_ADDRESS_PINCODE


def restate_foreign_pincodes(invoice_xml: str) -> str:
	"""Write 00000 over every foreign CAP the rendered file carries that SDI would refuse."""

	def restate(match: re.Match) -> str:
		if match.group(4).strip().upper() == "IT":
			return match.group(0)

		return match.group(1) + get_foreign_pincode(match.group(2)) + match.group(3) + match.group(4) + match.group(5)

	return ADDRESS_PATTERN.sub(restate, invoice_xml)
