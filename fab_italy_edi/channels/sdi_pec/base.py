from __future__ import annotations

import imaplib
import json
import re
import smtplib
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import make_msgid
from typing import Any, Mapping

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint

from fab_italy_edi import edi_lifecycle
from fab_italy_edi.channels.base import ChannelAdapter
from fab_italy_edi.document_utils import get_document_secret, get_document_value, normalize_identifier
from fab_italy_edi.sales_invoice_edi import sync_sales_invoice_tracking

XML_FILENAME_PATTERN = re.compile(r"\b[A-Z0-9][A-Z0-9._-]*\.xml(?:\.p7m)?\b", re.IGNORECASE)
MESSAGE_ID_PATTERN = re.compile(r"<([^>]+)>")
RECEIPT_CODE_PATTERN = re.compile(r"\b(NS|RC|MC|NE|DT|AT|EC)\b", re.IGNORECASE)
PEC_REQUIRED_CONFIGURATION_FIELDS = {
	"pec_username": _("PEC Username"),
	"pec_password": _("PEC Password"),
	"pec_imap_host": _("PEC IMAP Host"),
	"pec_imap_port": _("PEC IMAP Port"),
	"pec_smtp_host": _("PEC SMTP Host"),
	"pec_smtp_port": _("PEC SMTP Port"),
}


class SDIPECChannel(ChannelAdapter):
	adapter_key = "sdi_pec"
	default_recipient_address = "sdi01@pec.fatturapa.it"
	default_imap_folder = "INBOX"
	default_imap_search_criteria = "ALL"
	smtp_timeout = 30

	def validate_configuration(self, configuration: Mapping[str, Any]) -> list[str]:
		return [
			fieldname
			for fieldname in PEC_REQUIRED_CONFIGURATION_FIELDS
			if not normalize_identifier(configuration.get(fieldname))
		]

	def submit_outbound_invoice(
		self,
		provider,
		xml_content: str,
		*,
		configuration=None,
		document=None,
	) -> dict[str, Any]:
		runtime_configuration = self.build_runtime_configuration(configuration, provider)
		filename = self.build_attachment_filename(document)
		subject = self.build_outbound_subject(document, filename, runtime_configuration)
		from_address = self.get_sender_address(runtime_configuration)
		recipient_address = runtime_configuration["recipient_address"]
		message_id = make_msgid(idstring=(get_document_value(document, "name") if document else None))

		message = EmailMessage()
		message["From"] = from_address
		message["To"] = recipient_address
		message["Subject"] = subject
		message["Message-ID"] = message_id
		message.set_content(self.build_outbound_body(document, filename))
		message.add_attachment(
			xml_content.encode("utf-8"),
			maintype="application",
			subtype="xml",
			filename=filename,
		)

		with self.open_smtp_connection(runtime_configuration) as smtp_connection:
			smtp_connection.login(
				runtime_configuration["pec_username"],
				runtime_configuration["pec_password"],
			)
			smtp_connection.send_message(message)

		normalized_message_id = normalize_message_identifier(message_id)
		return {
			"uuid": normalized_message_id,
			"message_id": normalized_message_id,
			"recipient": recipient_address,
			"subject": subject,
		}

	def poll_outbound_statuses(self, configuration, provider) -> dict[str, int]:
		return self.process_mailbox(configuration, provider)

	def fetch_notifications(self, configuration, provider) -> dict[str, int]:
		# PEC notifications are already covered by mailbox polling above.
		return {"processed": 0, "updated": 0, "matched": 0, "unmatched": 0}

	def fetch_incoming_supplier_invoices(self, configuration, provider) -> dict[str, int]:
		return {"processed": 0, "created": 0, "updated": 0}

	def process_mailbox(self, configuration, provider) -> dict[str, int]:
		runtime_configuration = self.build_runtime_configuration(configuration, provider)
		processed = 0
		updated = 0
		matched = 0
		unmatched = 0

		imap_connection = self.open_imap_connection(runtime_configuration)
		try:
			status, _ = imap_connection.select(runtime_configuration["imap_folder"])
			if status != "OK":
				raise ValidationError(
					_("Could not open PEC mailbox folder {0}.").format(runtime_configuration["imap_folder"])
				)

			search_criteria = runtime_configuration["imap_search_criteria"].split()
			status, data = imap_connection.search(None, *search_criteria)
			if status != "OK":
				raise ValidationError(_("Could not search the PEC mailbox."))

			for mailbox_message_id in parse_imap_search_ids(data):
				raw_message = fetch_imap_message_bytes(imap_connection, mailbox_message_id)
				message = BytesParser(policy=policy.default).parsebytes(raw_message)
				processed += 1
				result = self.process_mailbox_message(
					configuration,
					provider,
					message,
					raw_message,
					mailbox_message_id=mailbox_message_id,
				)
				updated += int(result["updated"])
				matched += int(result["matched"])
				unmatched += int(not result["matched"])
		finally:
			try:
				imap_connection.logout()
			except Exception:
				pass

		return {
			"processed": processed,
			"updated": updated,
			"matched": matched,
			"unmatched": unmatched,
		}

	def process_mailbox_message(
		self,
		configuration,
		provider,
		message: Message,
		raw_message: bytes,
		*,
		mailbox_message_id: str | None = None,
	) -> dict[str, bool]:
		receipt_type = detect_receipt_type(message)
		if not receipt_type:
			return {"matched": False, "updated": False}

		document = find_matching_document_for_message(configuration, provider, message)
		if not document:
			return {"matched": False, "updated": False}

		external_message_id = normalize_message_identifier(message.get("Message-ID")) or (
			f"imap:{mailbox_message_id}" if mailbox_message_id else None
		)
		if not external_message_id:
			external_message_id = edi_lifecycle.build_receipt_message_id(
				"pec",
				document.name,
				receipt_type,
			)

		processing_notes = build_processing_notes(message, receipt_type)
		normalized_state = normalize_pec_receipt_state(receipt_type, message)
		payload_file = edi_lifecycle.persist_payload_artifact(
			document,
			f"pec-receipt-{external_message_id.replace(':', '-')}",
			raw_message.decode("utf-8", errors="replace"),
		)
		receipt_created = edi_lifecycle.append_edi_receipt(
			document,
			receipt_type=receipt_type,
			normalized_state=normalized_state,
			external_message_id=external_message_id,
			payload_file=payload_file,
			processing_notes=processing_notes,
			matched=1,
		)
		state_changed, activity_message = edi_lifecycle.reconcile_edi_document_states(
			document,
			transmission_state=normalized_state,
			receipt_state=normalized_state,
			last_error=processing_notes if normalized_state in {"rejected", "failed"} else None,
			event_label=f"received PEC receipt {receipt_type}",
			detail_text=processing_notes,
			always_log=receipt_created,
		)
		changed = receipt_created or state_changed
		if changed:
			document.save(ignore_permissions=True)
		if activity_message:
			sync_sales_invoice_tracking(document, activity_message=activity_message)
		return {"matched": True, "updated": changed}

	def build_runtime_configuration(self, configuration, provider) -> dict[str, Any]:
		if configuration is None:
			raise ValidationError(_("A company EDI Configuration is required for PEC transmission."))

		settings = get_provider_settings(provider)
		runtime_configuration = {
			"pec_username": normalize_identifier(get_document_value(configuration, "pec_username")),
			"pec_password": get_document_secret(configuration, "pec_password"),
			"pec_imap_host": normalize_identifier(get_document_value(configuration, "pec_imap_host")),
			"pec_imap_port": normalize_identifier(get_document_value(configuration, "pec_imap_port")),
			"pec_use_ssl_for_imap": cint(get_document_value(configuration, "pec_use_ssl_for_imap")),
			"pec_smtp_host": normalize_identifier(get_document_value(configuration, "pec_smtp_host")),
			"pec_smtp_port": normalize_identifier(get_document_value(configuration, "pec_smtp_port")),
			"pec_use_ssl_for_smtp": cint(get_document_value(configuration, "pec_use_ssl_for_smtp")),
			"sender_pec_address": normalize_identifier(
				get_document_value(configuration, "sender_pec_address")
			),
			"sender_email": normalize_identifier(get_document_value(configuration, "sender_email")),
			"recipient_address": normalize_identifier(settings.get("recipient_address"))
			or self.default_recipient_address,
			"imap_folder": normalize_identifier(settings.get("imap_folder")) or self.default_imap_folder,
			"imap_search_criteria": normalize_identifier(settings.get("imap_search_criteria"))
			or self.default_imap_search_criteria,
		}
		missing_fields = self.validate_configuration(runtime_configuration)
		if missing_fields:
			raise ValidationError(
				_("Set the PEC mailbox fields on EDI Configuration before using SDI via PEC: {0}.").format(
					", ".join(PEC_REQUIRED_CONFIGURATION_FIELDS[fieldname] for fieldname in missing_fields)
				)
			)
		return runtime_configuration

	def get_sender_address(self, runtime_configuration: Mapping[str, Any]) -> str:
		sender = runtime_configuration.get("sender_pec_address") or runtime_configuration.get("sender_email")
		sender = sender or runtime_configuration.get("pec_username")
		if not sender:
			raise ValidationError(_("Set Sender PEC Address or Sender Email before sending via PEC."))
		return str(sender)

	def build_attachment_filename(self, document) -> str:
		filename = normalize_identifier(get_document_value(document, "canonical_identifier"))
		filename = filename or normalize_identifier(get_document_value(document, "source_name")) or "edi-document"
		if not filename.lower().endswith(".xml"):
			filename = f"{filename}.xml"
		return filename

	def build_outbound_subject(
		self, document, filename: str, runtime_configuration: Mapping[str, Any]
	) -> str:
		source_name = normalize_identifier(get_document_value(document, "source_name")) or filename
		return _("FAB EDI PEC submission {0}").format(source_name)

	def build_outbound_body(self, document, filename: str) -> str:
		source_name = normalize_identifier(get_document_value(document, "source_name")) or _("unknown document")
		return _(
			"Attached is the FatturaPA XML generated by FAB EDI for {0}.\n\nFilename: {1}"
		).format(source_name, filename)

	def open_smtp_connection(self, runtime_configuration: Mapping[str, Any]):
		host = runtime_configuration["pec_smtp_host"]
		port = cint(runtime_configuration["pec_smtp_port"])
		if runtime_configuration.get("pec_use_ssl_for_smtp"):
			return smtplib.SMTP_SSL(host, port, timeout=self.smtp_timeout)

		return smtplib.SMTP(host, port, timeout=self.smtp_timeout)

	def open_imap_connection(self, runtime_configuration: Mapping[str, Any]):
		host = runtime_configuration["pec_imap_host"]
		port = cint(runtime_configuration["pec_imap_port"])
		if runtime_configuration.get("pec_use_ssl_for_imap"):
			connection = imaplib.IMAP4_SSL(host, port)
		else:
			connection = imaplib.IMAP4(host, port)
		connection.login(
			runtime_configuration["pec_username"],
			runtime_configuration["pec_password"],
		)
		return connection


def get_provider_settings(provider) -> dict[str, Any]:
	value = get_document_value(provider, "additional_settings")
	if isinstance(value, Mapping):
		return dict(value)
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return {}
		return json.loads(value)
	return {}


def parse_imap_search_ids(data: list[bytes] | tuple[bytes, ...]) -> list[str]:
	if not data:
		return []
	return [item for item in data[0].decode().split() if item]


def fetch_imap_message_bytes(imap_connection, mailbox_message_id: str) -> bytes:
	status, payload = imap_connection.fetch(mailbox_message_id, "(RFC822)")
	if status != "OK":
		raise ValidationError(_("Could not fetch PEC mailbox message {0}.").format(mailbox_message_id))
	for row in payload:
		if isinstance(row, tuple) and len(row) > 1 and isinstance(row[1], (bytes, bytearray)):
			return bytes(row[1])
	raise ValidationError(_("PEC mailbox message {0} had no RFC822 payload.").format(mailbox_message_id))


def find_matching_document_for_message(configuration, provider, message: Message):
	for reference_id in extract_reference_identifiers(message):
		docname = frappe.db.get_value(
			"EDI Document",
			{
				"company": configuration.company,
				"provider": provider.name,
				"external_submission_id": reference_id,
			},
			"name",
		)
		if docname:
			return frappe.get_doc("EDI Document", docname)

	for canonical_identifier in extract_canonical_identifiers(message):
		docname = frappe.db.get_value(
			"EDI Document",
			{
				"company": configuration.company,
				"provider": provider.name,
				"canonical_identifier": canonical_identifier,
			},
			"name",
		)
		if docname:
			return frappe.get_doc("EDI Document", docname)

	return None


def extract_reference_identifiers(message: Message) -> list[str]:
	identifiers: list[str] = []
	for header_name in ("In-Reply-To", "References"):
		header_value = decode_header_value(message.get(header_name))
		if not header_value:
			continue
		for match in MESSAGE_ID_PATTERN.findall(header_value):
			normalized = normalize_message_identifier(match)
			if normalized and normalized not in identifiers:
				identifiers.append(normalized)
	return identifiers


def extract_canonical_identifiers(message: Message) -> list[str]:
	candidates: list[str] = []
	searchable_parts = [decode_header_value(message.get("Subject")), extract_text_body(message)]
	searchable_parts.extend(get_attachment_filenames(message))
	for part in searchable_parts:
		for match in XML_FILENAME_PATTERN.findall(part or ""):
			for candidate in canonical_identifier_variants(match):
				if candidate not in candidates:
					candidates.append(candidate)
	return candidates


def canonical_identifier_variants(value: str) -> list[str]:
	text = str(value).strip()
	variants = [text]
	if text.lower().endswith(".p7m"):
		variants.append(text[:-4])
	return variants


def get_attachment_filenames(message: Message) -> list[str]:
	filenames: list[str] = []
	for part in message.iter_attachments():
		filename = decode_header_value(part.get_filename())
		if filename:
			filenames.append(filename)
	return filenames


def detect_receipt_type(message: Message) -> str | None:
	text = build_message_search_text(message)
	if "accettazione" in text:
		return "pec_acceptance"
	if "avvenuta consegna" in text or "consegna" in text:
		return "pec_delivery"

	match = RECEIPT_CODE_PATTERN.search(text.upper())
	if match:
		return match.group(1).upper()
	return None


def normalize_pec_receipt_state(receipt_type: str, message: Message) -> str:
	normalized_type = (receipt_type or "").strip().lower()
	if normalized_type == "pec_acceptance":
		return "sent"
	if normalized_type == "pec_delivery":
		return "delivered"
	code = (normalize_identifier(receipt_type) or "").upper()
	text = build_message_search_text(message).lower()
	if code == "NS" or any(token in text for token in ("scarto", "rejected", "reject")):
		return "rejected"
	if code in {"RC", "MC", "AT"} or any(token in text for token in ("consegna", "delivered")):
		return "delivered"
	if code in {"DT", "EC"} or any(token in text for token in ("accepted", "accett", "termini")):
		return "accepted"
	if any(token in text for token in ("failed", "errore", "error")):
		return "failed"
	if any(token in text for token in ("cancel", "annull")):
		return "cancelled"
	if any(token in text for token in ("queued", "pending", "attesa")):
		return "queued"
	return "unknown_pending"


def build_processing_notes(message: Message, receipt_type: str) -> str:
	parts = [
		f"receipt_type: {receipt_type}",
		f"from: {decode_header_value(message.get('From')) or '-'}",
		f"subject: {decode_header_value(message.get('Subject')) or '-'}",
	]
	date_header = decode_header_value(message.get("Date"))
	if date_header:
		parts.append(f"date: {date_header}")
	attachment_filenames = get_attachment_filenames(message)
	if attachment_filenames:
		parts.append(f"attachments: {', '.join(attachment_filenames)}")
	return "\n".join(parts)


def build_message_search_text(message: Message) -> str:
	parts = [
		decode_header_value(message.get("Subject")),
		extract_text_body(message),
		" ".join(get_attachment_filenames(message)),
	]
	return " ".join(part for part in parts if part).lower()


def extract_text_body(message: Message) -> str:
	if message.is_multipart():
		parts: list[str] = []
		for child in message.walk():
			if child.get_content_disposition() == "attachment":
				continue
			if child.get_content_type() not in {"text/plain", "text/html"}:
				continue
			payload = child.get_payload(decode=True) or b""
			charset = child.get_content_charset() or "utf-8"
			parts.append(payload.decode(charset, errors="replace"))
		return "\n".join(parts)

	payload = message.get_payload(decode=True) or b""
	charset = message.get_content_charset() or "utf-8"
	return payload.decode(charset, errors="replace")


def decode_header_value(value: Any) -> str:
	if not value:
		return ""
	return str(make_header(decode_header(str(value))))


def normalize_message_identifier(value: Any) -> str | None:
	identifier = normalize_identifier(value)
	if not identifier:
		return None
	if identifier.startswith("<") and identifier.endswith(">"):
		return identifier[1:-1]
	return identifier
