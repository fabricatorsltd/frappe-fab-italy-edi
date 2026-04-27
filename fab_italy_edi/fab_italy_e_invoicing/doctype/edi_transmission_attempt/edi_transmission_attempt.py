from frappe.model.document import Document


class EDITransmissionAttempt(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempt_number: DF.Int | None
		external_reference_id: DF.Data | None
		external_status_code: DF.Data | None
		external_status_message: DF.SmallText | None
		request_payload: DF.Attach | None
		response_payload: DF.Attach | None
		retryable: DF.Check
		transport_status: DF.Select | None
	# end: auto-generated types
