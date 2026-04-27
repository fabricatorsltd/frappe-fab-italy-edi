from frappe.model.document import Document


class EDIChannel(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		capabilities: DF.JSON | None
		channel_key: DF.Data
		channel_name: DF.Data
		enabled: DF.Check
		handler_path: DF.Data | None
		supports_inbound: DF.Check
		supports_outbound: DF.Check
		supports_polling: DF.Check
		supports_webhooks: DF.Check
	# end: auto-generated types
