from frappe.model.document import Document

from fab_italy_edi.backends import get_provider_adapter


class EDIProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		additional_settings: DF.JSON | None
		adapter_key: DF.Data
		api_key: DF.Password | None
		api_secret: DF.Password | None
		auth_mode: DF.Select | None
		channel: DF.Link
		enabled: DF.Check
		environment: DF.Select | None
		endpoint_url: DF.Data | None
		password: DF.Password | None
		provider_name: DF.Data
		status_url: DF.Data | None
		use_environment_default_endpoint: DF.Check
		use_status_polling: DF.Check
		use_webhooks: DF.Check
		username: DF.Data | None
		webhook_path: DF.Data | None
	# end: auto-generated types

	def validate(self):
		adapter = get_provider_adapter(self)
		normalizer = getattr(adapter, "normalize_provider_document", None) if adapter else None
		if callable(normalizer):
			normalizer(self)
