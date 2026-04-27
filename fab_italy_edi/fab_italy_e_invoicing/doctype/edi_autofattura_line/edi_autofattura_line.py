from frappe.model.document import Document


class EDIAutofatturaLine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		description: DF.SmallText | None
		nature: DF.Data | None
		quantity: DF.Float
		reference_law: DF.SmallText | None
		tax_rate: DF.Float
		total_price: DF.Float
		unit_of_measure: DF.Data | None
		unit_price: DF.Float
	# end: auto-generated types

