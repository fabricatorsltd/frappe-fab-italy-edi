from __future__ import annotations

from typing import Any
import json
from pathlib import Path
import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint

from fab_italy_edi.automation import automation_user_context
from fab_italy_edi.backends import get_adapter_class, get_provider_adapter
from fab_italy_edi.document_utils import normalize_identifier
from fab_italy_edi.edi_lifecycle import (
	append_edi_receipt,
	build_receipt_message_id,
	find_receipt_row,
	persist_payload_artifact,
	reconcile_edi_document_states,
)
from fab_italy_edi.provider_sync import (
	find_incoming_edi_configuration_for_invoice,
	sync_notification,
	sync_outbound_invoice,
	upsert_incoming_invoice_document,
)
from fab_italy_edi.sales_invoice_edi import (
	build_send_failure_message,
	build_send_success_message,
	sync_sales_invoice_tracking,
)


ACTIVE_TRANSMISSION_STATES = {"queued", "sending", "sent", "delivered", "accepted"}
OUTBOUND_SEND_QUEUE = "long"


@frappe.whitelist()
def send_sales_invoice_to_sdi(docname: str) -> dict[str, str]:
	invoice = frappe.get_doc("Sales Invoice", docname)
	frappe.has_permission("Sales Invoice", doc=invoice, throw=True)

	if cint(invoice.docstatus) != 1:
		raise ValidationError(_("Only submitted Sales Invoices can be sent to SDI."))

	configuration = get_enabled_edi_configuration(invoice.company)
	provider = get_enabled_provider(configuration.default_provider)
	adapter = get_provider_adapter(provider)
	if not adapter or not callable(getattr(adapter, "submit_outbound_invoice", None)):
		raise ValidationError(
			_("Provider {0} does not support outbound SDI submission yet.").format(provider.name)
		)
	if callable(getattr(adapter, "ensure_outbound_submission_ready", None)):
		adapter.ensure_outbound_submission_ready(configuration=configuration, provider=provider)

	document = get_or_create_outbound_document(invoice, provider)
	raise_if_document_already_transmitting(document, source_label=_("Sales Invoice"))

	generated_file = generate_sales_invoice_xml(invoice)
	return prepare_document_for_outbound_queue(document, generated_file)


def prepare_document_for_outbound_queue(document, generated_file) -> dict[str, str]:
	document.generated_xml = generated_file.file_url
	document.validation_state = "valid"
	document.external_submission_id = None
	reconcile_edi_document_states(
		document,
		transmission_state="queued",
		receipt_state="queued",
		canonical_identifier=generated_file.file_name,
		last_error=None,
	)
	document.latest_receipt_state = "queued"
	document.save(ignore_permissions=True)
	sync_outbound_source_tracking(document)
	enqueue_outbound_send_job(document.name)
	return {
		"edi_document": document.name,
		"external_submission_id": document.external_submission_id,
		"transmission_state": document.transmission_state,
		"latest_receipt_state": document.latest_receipt_state,
	}


def enqueue_outbound_send_job(document_name: str) -> None:
	frappe.enqueue(
		"fab_italy_edi.api.run_outbound_send_job",
		queue=OUTBOUND_SEND_QUEUE,
		job_name=f"fab_italy_edi:outbound-send:{document_name}",
		enqueue_after_commit=True,
		document_name=document_name,
	)


def run_outbound_send_job(document_name: str) -> dict[str, Any] | None:
	document = frappe.get_doc("EDI Document", document_name)
	if document.external_submission_id and document.transmission_state in ACTIVE_TRANSMISSION_STATES:
		return {
			"edi_document": document.name,
			"external_submission_id": document.external_submission_id,
			"transmission_state": document.transmission_state,
			"latest_receipt_state": document.latest_receipt_state,
		}

	with automation_user_context():
		return submit_outbound_document(document)


def submit_outbound_document(document) -> dict[str, Any]:
	provider = get_enabled_provider(document.provider)
	adapter = get_provider_adapter(provider)
	if not adapter or not callable(getattr(adapter, "submit_outbound_invoice", None)):
		raise ValidationError(
			_("Provider {0} does not support outbound SDI submission yet.").format(provider.name)
		)

	configuration = get_enabled_edi_configuration(document.company)
	attempt_number = len(document.transmission_attempts or []) + 1
	reconcile_edi_document_states(
		document,
		transmission_state="sending",
		last_error=None,
	)
	document.save(ignore_permissions=True)
	sync_outbound_source_tracking(document)

	request_payload = None
	try:
		if callable(getattr(adapter, "ensure_outbound_submission_ready", None)):
			adapter.ensure_outbound_submission_ready(configuration=configuration, provider=provider)
		xml_content = read_site_file(document.generated_xml)
		request_payload = persist_payload_artifact(
			document,
			f"outbound-request-{document.source_name}-{attempt_number}",
			xml_content,
		)
		response_data = adapter.submit_outbound_invoice(
			provider=provider,
			configuration=configuration,
			document=document,
			xml_content=xml_content,
		)
	except Exception as exc:
		append_transmission_attempt(
			document,
			attempt_number=attempt_number,
			transport_status="failed",
			request_payload=request_payload,
			external_status_message=str(exc),
			retryable=1,
		)
		reconcile_edi_document_states(
			document,
			transmission_state="failed",
			receipt_state="failed",
			last_error=str(exc),
		)
		document.save(ignore_permissions=True)
		sync_outbound_source_tracking(
			document,
			activity_message=build_outbound_send_failure_message(document, exc),
		)
		return {
			"edi_document": document.name,
			"external_submission_id": document.external_submission_id,
			"transmission_state": document.transmission_state,
			"latest_receipt_state": document.latest_receipt_state,
			"last_error": document.last_error,
		}

	response_payload = persist_payload_artifact(
		document,
		f"outbound-response-{document.source_name}-{attempt_number}",
		response_data,
	)
	external_submission_id = response_data["uuid"]
	append_transmission_attempt(
		document,
		attempt_number=attempt_number,
		transport_status="success",
		request_payload=request_payload,
		response_payload=response_payload,
		external_reference_id=external_submission_id,
		external_status_message=_("Queued at provider"),
		retryable=0,
	)
	reconcile_edi_document_states(
		document,
		transmission_state="queued",
		receipt_state="queued",
		external_submission_id=external_submission_id,
		last_error=None,
	)
	document.latest_receipt_state = "queued"
	document.save(ignore_permissions=True)
	sync_outbound_source_tracking(
		document,
		activity_message=build_outbound_send_success_message(document, external_submission_id),
	)
	return {
		"edi_document": document.name,
		"external_submission_id": external_submission_id,
		"transmission_state": document.transmission_state,
		"latest_receipt_state": document.latest_receipt_state,
	}


@frappe.whitelist(allow_guest=True)
def receive_openapi_callback() -> dict[str, bool]:
	if frappe.request.method != "POST":
		raise ValidationError(_("OpenAPI callbacks only accept POST requests."))

	payload = json.loads(frappe.request.get_data(as_text=True) or "{}")
	adapter_class = get_adapter_class("openapi")
	if not adapter_class:
		raise ValidationError(_("No OpenAPI backend is registered."))
	adapter = adapter_class()
	event = adapter.extract_callback_event(payload)

	with automation_user_context():
		if event == "customer-notification":
			notification = adapter.extract_callback_notification(payload)
			document = get_document_by_external_submission_id(
				adapter.get_notification_external_submission_id(notification)
			)
			if document:
				sync_notification(document, adapter, notification)
		elif event == "customer-invoice":
			invoice = adapter.extract_callback_outbound_invoice(payload)
			document = get_document_by_external_submission_id(
				adapter.get_outbound_invoice_external_submission_id(invoice)
			)
			if document:
				sync_outbound_invoice(document, adapter, invoice)
		elif event == "supplier-invoice":
			invoice = adapter.extract_callback_supplier_invoice(payload)
			configuration = find_incoming_edi_configuration_for_invoice(adapter, invoice)
			if configuration and configuration.default_provider:
				provider = get_enabled_provider(configuration.default_provider)
				if normalize_identifier(getattr(provider, "adapter_key", None)) == getattr(
					adapter, "adapter_key", None
				):
					upsert_incoming_invoice_document(configuration, provider, adapter, invoice)
		elif event in {"legal-storage-receipt", "legal-storage-missing-vat"}:
			handle_openapi_legal_storage_event(payload, event)

	frappe.local.response["http_status_code"] = 202
	return {"ok": True}


def handle_openapi_legal_storage_event(payload: dict[str, Any], event: str) -> None:
	data = payload.get("data") if isinstance(payload, dict) else None
	data = data if isinstance(data, dict) else {}
	document = get_document_by_external_submission_id(normalize_identifier(data.get("object_id")))
	if not document:
		return

	external_message_id = normalize_identifier(data.get("uuid")) or build_receipt_message_id(
		event, data.get("object_id"), data.get("preserved_object_id")
	)
	if find_receipt_row(document, external_message_id, receipt_type=event):
		return

	payload_file = persist_payload_artifact(
		document,
		f"openapi-{event}",
		payload,
	)
	processing_notes = build_openapi_legal_storage_processing_notes(event, data)
	receipt_created = append_edi_receipt(
		document,
		receipt_type=event,
		normalized_state=document.latest_receipt_state,
		external_message_id=external_message_id,
		payload_file=payload_file,
		processing_notes=processing_notes,
	)
	state_changed, activity_message = reconcile_edi_document_states(
		document,
		event_label=f"received OpenAPI {event}",
		detail_text=processing_notes,
		always_log=receipt_created,
	)
	if receipt_created or state_changed:
		document.save(ignore_permissions=True)
	if activity_message:
		sync_outbound_source_tracking(document, activity_message=activity_message)


def build_openapi_legal_storage_processing_notes(event: str, data: dict[str, Any]) -> str:
	parts = [f"event: {event}"]
	for key in (
		"status",
		"object_type",
		"object_id",
		"preserved_object_id",
		"receipt_received_at",
		"created_at",
		"message",
	):
		value = data.get(key)
		if value not in (None, ""):
			parts.append(f"{key}: {value}")
	return "\n".join(parts)


def get_enabled_edi_configuration(company: str):
	if not company:
		raise ValidationError(_("Sales Invoice is missing a company."))

	try:
		configuration = frappe.get_doc("EDI Configuration", company)
	except frappe.DoesNotExistError as exc:
		raise ValidationError(
			_("Create an EDI Configuration for company {0} before sending invoices to SDI.").format(company)
		) from exc
	if not configuration.enabled:
		raise ValidationError(_("EDI Configuration {0} is disabled.").format(configuration.name))
	if not configuration.default_provider:
		raise ValidationError(
			_("EDI Configuration {0} must define a default provider before sending to SDI.").format(
				configuration.name
			)
		)

	return configuration


def get_document_by_external_submission_id(external_submission_id: str | None):
	external_submission_id = normalize_identifier(external_submission_id)
	if not external_submission_id:
		return None

	docname = frappe.db.get_value("EDI Document", {"external_submission_id": external_submission_id})
	if not docname:
		return None

	return frappe.get_doc("EDI Document", docname)


def get_enabled_provider(provider_name: str):
	try:
		provider = frappe.get_doc("EDI Provider", provider_name)
	except frappe.DoesNotExistError as exc:
		raise ValidationError(_("EDI Provider {0} does not exist.").format(provider_name)) from exc
	if not provider.enabled:
		raise ValidationError(_("EDI Provider {0} is disabled.").format(provider.name))
	return provider


def get_or_create_outbound_document(invoice, provider):
	document_kind = "credit_note" if cint(invoice.is_return) else "invoice"
	docname = frappe.db.get_value(
		"EDI Document",
		{
			"source_doctype": invoice.doctype,
			"source_name": invoice.name,
			"document_kind": document_kind,
		},
	)

	if docname:
		document = frappe.get_doc("EDI Document", docname)
	else:
		document = frappe.get_doc(
			{
				"doctype": "EDI Document",
				"source_doctype": invoice.doctype,
				"source_name": invoice.name,
				"company": invoice.company,
				"document_kind": document_kind,
				"party_name": invoice.customer_name or invoice.customer,
				"channel": provider.channel,
				"provider": provider.name,
				"validation_state": "draft",
				"transmission_state": "draft",
				"idempotency_key": build_outbound_idempotency_key(invoice),
			}
		)
		document.insert(ignore_permissions=True)

	document.party_name = invoice.customer_name or invoice.customer
	document.channel = provider.channel
	document.provider = provider.name
	return document


def raise_if_document_already_transmitting(document, *, source_label: str) -> None:
	if document.transmission_state not in ACTIVE_TRANSMISSION_STATES:
		return
	if document.external_submission_id:
		raise ValidationError(
			_("{0} {1} is already queued or sent to SDI as {2}.").format(
				source_label,
				document.source_name,
				document.external_submission_id,
			)
		)
	raise ValidationError(_("{0} {1} is already queued or being sent to SDI.").format(source_label, document.source_name))


def sync_outbound_source_tracking(document, *, activity_message: str | None = None) -> None:
	if document.source_doctype == "Sales Invoice":
		sync_sales_invoice_tracking(document, activity_message=activity_message)
		return
	if document.source_doctype != "Autofattura":
		return

	from fab_italy_edi import autofattura as autofattura_module

	autofattura_document = autofattura_module.get_autofattura(document.source_name)
	autofattura_module.sync_autofattura_from_transport_document(autofattura_document, document, save=True)
	if activity_message and getattr(autofattura_document, "source_purchase_invoice", None):
		purchase_invoice = autofattura_module.get_purchase_invoice_for_autofattura(
			autofattura_document.source_purchase_invoice
		)
		purchase_invoice.add_comment("Info", activity_message)


def build_outbound_send_success_message(document, external_submission_id: str) -> str:
	transport_label = get_transport_label(document)
	if document.source_doctype == "Autofattura":
		return _("Queued autofattura {0} to {1} as {2}.").format(
			document.source_name, transport_label, external_submission_id
		)
	return build_send_success_message(external_submission_id, transport_label=transport_label)


def build_outbound_send_failure_message(document, error_text: Any) -> str:
	if document.source_doctype == "Autofattura":
		return _("Autofattura SDI send failed: {0}").format(str(error_text).splitlines()[0].strip())
	return build_send_failure_message(error_text)


def get_transport_label(document) -> str:
	channel = normalize_identifier(getattr(document, "channel", None))
	if channel == "sdi_proxy":
		return _("SDI proxy")
	if channel == "sdi_pec":
		return _("PEC")
	return _("SDI")


def build_outbound_idempotency_key(invoice) -> str:
	return f"outbound:{invoice.doctype}:{invoice.name}"


def read_site_file(file_url: str) -> str:
	if not file_url:
		raise ValidationError(_("Generated XML is missing a file URL."))
	return Path(frappe.get_site_path(file_url.lstrip("/"))).read_text(encoding="utf-8")


def generate_sales_invoice_xml(invoice):
	from erpnext.regional.italy.utils import prepare_and_attach_invoice

	return prepare_and_attach_invoice(invoice, replace=True)


def append_transmission_attempt(
	document,
	*,
	attempt_number: int,
	transport_status: str,
	request_payload: str | None = None,
	response_payload: str | None = None,
	external_reference_id: str | None = None,
	external_status_message: str | None = None,
	retryable: int = 0,
):
	document.append(
		"transmission_attempts",
		{
			"attempt_number": attempt_number,
			"transport_status": transport_status,
			"retryable": retryable,
			"external_reference_id": external_reference_id,
			"external_status_message": external_status_message,
			"request_payload": request_payload,
			"response_payload": response_payload,
		},
	)
