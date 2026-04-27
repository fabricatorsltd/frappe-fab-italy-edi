import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from fab_italy_edi.channels.sdi_pec.base import PEC_REQUIRED_CONFIGURATION_FIELDS, SDIPECChannel
from fab_italy_edi.inbound_tax_setup import ensure_standard_inbound_natura_configuration


MIN_POLLING_INTERVAL_MINUTES = 5
MAX_POLLING_INTERVAL_MINUTES = 24 * 60
POLLING_INTERVAL_FIELDS = {
	"outbound_status_polling_interval_mins": "Outbound Status Polling Interval",
	"incoming_invoice_polling_interval_mins": "Incoming Supplier Invoice Polling Interval",
	"notification_polling_interval_mins": "Notification Polling Interval",
}


class EDIConfiguration(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_inbound_tax_mapping.edi_inbound_tax_mapping import (
			EDIInboundTaxMapping,
		)
		from frappe.types import DF

		company: DF.Link
		default_channel: DF.Link | None
		default_provider: DF.Link | None
		autofattura_naming_series: DF.Data | None
		enabled: DF.Check
		auto_refresh_enabled: DF.Check
		incoming_invoice_polling_interval_mins: DF.Int
		last_incoming_invoice_polling_at: DF.Datetime | None
		last_notification_polling_at: DF.Datetime | None
		last_outbound_status_polling_at: DF.Datetime | None
		inbound_tax_mappings: DF.Table[EDIInboundTaxMapping]
		notes: DF.SmallText | None
		notification_polling_interval_mins: DF.Int
		outbound_status_polling_interval_mins: DF.Int
		pec_imap_host: DF.Data | None
		pec_imap_port: DF.Int | None
		pec_password: DF.Password | None
		pec_smtp_host: DF.Data | None
		pec_smtp_port: DF.Int | None
		pec_use_ssl_for_imap: DF.Check
		pec_use_ssl_for_smtp: DF.Check
		pec_username: DF.Data | None
		recipient_code_fallback: DF.Data | None
		sender_email: DF.Data | None
		sender_fiscal_code: DF.Data | None
		sender_pec_address: DF.Data | None
		sender_vat_id: DF.Data | None
	# end: auto-generated types

	def before_validate(self):
		if self.is_new():
			ensure_standard_inbound_natura_configuration(self)

	def validate(self):
		for fieldname, label in POLLING_INTERVAL_FIELDS.items():
			validate_polling_interval(label, self.get(fieldname))
		validate_inbound_tax_mappings(self.inbound_tax_mappings or [])
		validate_pec_mailbox_settings(self)


def validate_polling_interval(label: str, value: object) -> None:
	interval = cint(value)
	if interval < MIN_POLLING_INTERVAL_MINUTES:
		frappe.throw(
			_("{0} must be at least {1} minutes.").format(label, MIN_POLLING_INTERVAL_MINUTES)
		)

	if interval > MAX_POLLING_INTERVAL_MINUTES:
		frappe.throw(
			_("{0} cannot exceed {1} minutes.").format(label, MAX_POLLING_INTERVAL_MINUTES)
		)

	if interval % MIN_POLLING_INTERVAL_MINUTES != 0:
		frappe.throw(
			_("{0} must be a multiple of {1} minutes.").format(label, MIN_POLLING_INTERVAL_MINUTES)
		)


def validate_inbound_tax_mappings(rows) -> None:
	seen = set()
	for row in rows:
		key = (round(flt(row.get("tax_rate")), 6), (row.get("nature") or "").strip().upper())
		if key in seen:
			frappe.throw(
				_("Duplicate inbound tax mapping for rate {0} and nature {1}.").format(
					row.get("tax_rate") or 0,
					row.get("nature") or _("blank"),
				)
			)
		seen.add(key)


def validate_pec_mailbox_settings(document) -> None:
	if not requires_pec_mailbox(document):
		return

	missing_labels = []
	for fieldname, label in PEC_REQUIRED_CONFIGURATION_FIELDS.items():
		if normalize_configuration_value(document, fieldname):
			continue
		missing_labels.append(label)

	if missing_labels:
		frappe.throw(
			_("Set the PEC mailbox fields before using SDI via PEC: {0}.").format(", ".join(missing_labels))
		)


def requires_pec_mailbox(document) -> bool:
	provider_name = document.get("default_provider")
	if not provider_name:
		return False

	provider = frappe.db.get_value(
		"EDI Provider",
		provider_name,
		["channel", "adapter_key"],
		as_dict=True,
	)
	if not provider:
		return False
	return provider.get("adapter_key") == SDIPECChannel.adapter_key or provider.get("channel") == "sdi_pec"


def normalize_configuration_value(document, fieldname: str) -> str:
	get_password = getattr(document, "get_password", None)
	if fieldname == "pec_password" and callable(get_password):
		value = get_password(fieldname, raise_exception=False)
	else:
		value = document.get(fieldname)
	text = str(value or "").strip()
	return text
