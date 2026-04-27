from frappe.model.document import Document


class EDIReceipt(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		external_message_id: DF.Data | None
		matched: DF.Check
		normalized_state: DF.Select | None
		payload_file: DF.Attach | None
		processing_notes: DF.SmallText | None
		receipt_type: DF.Data | None
	# end: auto-generated types
