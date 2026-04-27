# Copyright (c) 2026, fabricators and contributors

from frappe.model.document import Document


class EDIInboundTaxMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_head: DF.Link
		nature: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		tax_rate: DF.Float | None
	# end: auto-generated types

	pass
