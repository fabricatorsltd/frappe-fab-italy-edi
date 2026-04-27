from __future__ import annotations

from typing import Any, Mapping

import frappe
from frappe import _
from frappe.exceptions import ValidationError

from fab_italy_edi import edi_lifecycle
from fab_italy_edi.document_utils import normalize_identifier, normalize_vat_code
from fab_italy_edi.edi_lifecycle import persist_payload_artifact
from fab_italy_edi.purchase_invoice_import import ensure_purchase_invoice_review_draft
from fab_italy_edi.sales_invoice_edi import sync_sales_invoice_tracking


def sync_outbound_invoice(document, adapter, invoice: Mapping[str, Any]) -> bool:
	normalized = adapter.normalize_outbound_invoice(invoice)
	receipt_state = edi_lifecycle.normalize_state(normalized.get("receipt_state")) or "unknown_pending"
	transmission_state = edi_lifecycle.normalize_state(normalized.get("transmission_state")) or receipt_state
	processing_notes = normalized.get("processing_notes")
	receipt_created = False

	current_receipt_state = edi_lifecycle.normalize_state(getattr(document, "latest_receipt_state", None))
	if receipt_state == current_receipt_state or edi_lifecycle.should_apply_state_transition(
		current_receipt_state,
		receipt_state,
		progress_order=edi_lifecycle.RECEIPT_STATE_PROGRESS,
	):
		receipt_message_id = normalize_identifier(normalized.get("receipt_message_id"))
		if receipt_message_id and not edi_lifecycle.find_receipt_row(
			document,
			receipt_message_id,
			receipt_type=normalized.get("receipt_type"),
		):
			payload_file = persist_payload_artifact(
				document,
				normalized.get("payload_prefix") or "provider-invoice-status",
				invoice,
			)
			receipt_created = edi_lifecycle.append_edi_receipt(
				document,
				receipt_type=normalized.get("receipt_type") or "provider_invoice_status",
				normalized_state=receipt_state,
				matched=1,
				external_message_id=receipt_message_id,
				payload_file=payload_file,
				processing_notes=processing_notes,
			)

	state_changed, activity_message = edi_lifecycle.reconcile_edi_document_states(
		document,
		transmission_state=transmission_state,
		receipt_state=receipt_state,
		external_submission_id=normalized.get("external_submission_id"),
		canonical_identifier=normalized.get("canonical_identifier"),
		last_error=processing_notes if transmission_state in {"rejected", "failed"} else None,
		event_label=normalized.get("event_label") or "received provider update",
		detail_text=processing_notes,
		always_log=receipt_created,
	)
	changed = receipt_created or state_changed
	if changed:
		document.save(ignore_permissions=True)
	if activity_message:
		sync_sales_invoice_tracking(document, activity_message=activity_message)
	return changed


def sync_notification(document, adapter, notification: Mapping[str, Any]) -> bool:
	normalized = adapter.normalize_notification(notification)
	external_message_id = normalize_identifier(normalized.get("external_message_id"))
	receipt_state = edi_lifecycle.normalize_state(normalized.get("receipt_state")) or "unknown_pending"
	transmission_state = edi_lifecycle.normalize_state(normalized.get("transmission_state")) or receipt_state
	processing_notes = normalized.get("processing_notes")
	receipt_created = False

	if external_message_id and not edi_lifecycle.find_receipt_row(document, external_message_id):
		payload_file = persist_payload_artifact(
			document,
			normalized.get("payload_prefix") or "provider-notification",
			notification,
		)
		receipt_created = edi_lifecycle.append_edi_receipt(
			document,
			receipt_type=normalized.get("receipt_type") or "notification",
			normalized_state=receipt_state,
			matched=1,
			external_message_id=external_message_id,
			payload_file=payload_file,
			processing_notes=processing_notes,
		)
	state_changed, activity_message = edi_lifecycle.reconcile_edi_document_states(
		document,
		transmission_state=transmission_state,
		receipt_state=receipt_state,
		last_error=processing_notes if transmission_state in {"rejected", "failed"} else None,
		event_label=normalized.get("event_label") or "received provider notification",
		detail_text=processing_notes,
		always_log=receipt_created,
	)
	changed = receipt_created or state_changed
	if changed:
		document.save(ignore_permissions=True)
	if activity_message:
		sync_sales_invoice_tracking(document, activity_message=activity_message)
	return changed


def upsert_incoming_invoice_document(configuration, provider, adapter, invoice: Mapping[str, Any]) -> dict[str, bool]:
	normalized = adapter.normalize_incoming_invoice(invoice)
	invoice_uuid = normalize_identifier(normalized.get("external_submission_id"))
	if not invoice_uuid:
		raise ValidationError(_("Provider returned an incoming invoice without an external identifier."))

	docname = frappe.db.get_value(
		"EDI Document",
		{
			"company": configuration.company,
			"provider": provider.name,
			"document_kind": "supplier_invoice_import",
			"external_submission_id": invoice_uuid,
		},
	)
	created = False
	changed = False

	if docname:
		document = frappe.get_doc("EDI Document", docname)
	else:
		document = frappe.get_doc(
			{
				"doctype": "EDI Document",
				"source_doctype": "EDI Configuration",
				"source_name": configuration.name,
				"company": configuration.company,
				"document_kind": "supplier_invoice_import",
				"party_name": normalized.get("party_name"),
				"channel": provider.channel,
				"provider": provider.name,
				"validation_state": "not_validated",
				"transmission_state": normalized.get("transmission_state") or "ready",
				"latest_receipt_state": normalized.get("receipt_state") or "delivered",
				"canonical_identifier": normalized.get("canonical_identifier"),
				"idempotency_key": f"incoming:{invoice_uuid}",
				"external_submission_id": invoice_uuid,
			}
		)
		document.insert(ignore_permissions=True)
		created = True
		changed = True

	party_name = normalize_identifier(normalized.get("party_name"))
	if party_name and document.party_name != party_name:
		document.party_name = party_name
		changed = True

	canonical_identifier = normalize_identifier(normalized.get("canonical_identifier"))
	if canonical_identifier and document.canonical_identifier != canonical_identifier:
		document.canonical_identifier = canonical_identifier
		changed = True

	if document.transmission_state not in {"ready", "imported"}:
		document.transmission_state = normalized.get("transmission_state") or "ready"
		changed = True

	if document.latest_receipt_state != (normalized.get("receipt_state") or "delivered"):
		document.latest_receipt_state = normalized.get("receipt_state") or "delivered"
		changed = True

	payload = normalized.get("payload")
	if payload and adapter.is_xml_payload(payload) and not document.source_xml:
		document.source_xml = persist_payload_artifact(
			document,
			normalized.get("payload_prefix") or "provider-incoming",
			payload,
			fieldname="source_xml",
		)
		changed = True

	if not edi_lifecycle.find_receipt_row(document, invoice_uuid):
		payload_file = None
		if payload and not adapter.is_xml_payload(payload):
			payload_file = persist_payload_artifact(
				document,
				normalized.get("payload_prefix") or "provider-incoming",
				invoice,
			)
		document.append(
			"receipts",
			{
				"receipt_type": normalized.get("receipt_type") or "supplier_invoice_fetched",
				"normalized_state": normalized.get("receipt_state") or "delivered",
				"matched": 1,
				"external_message_id": invoice_uuid,
				"payload_file": payload_file,
				"processing_notes": normalized.get("processing_notes"),
			},
		)
		changed = True

	if changed:
		document.last_error = None
		document.save(ignore_permissions=True)

	ensure_purchase_invoice_review_draft(document)
	return {"created": created, "updated": changed and not created}


def find_incoming_edi_configuration_for_invoice(adapter, invoice: Mapping[str, Any] | None):
	if not invoice:
		return None

	normalized = adapter.normalize_incoming_invoice(invoice)
	recipient_identifiers = {
		normalize_vat_code(value) or normalize_identifier(value)
		for value in normalized.get("recipient_identifiers") or []
	}
	recipient_identifiers.discard(None)
	if not recipient_identifiers:
		return None

	rows = frappe.get_all(
		"EDI Configuration",
		filters={"enabled": 1},
		fields=["name", "company", "default_provider", "sender_vat_id", "sender_fiscal_code"],
	)
	for row in rows:
		candidates = {
			normalize_vat_code(row.get("sender_vat_id")),
			normalize_identifier(row.get("sender_fiscal_code")),
			normalize_vat_code(frappe.get_cached_value("Company", row["company"], "tax_id")),
			normalize_identifier(frappe.get_cached_value("Company", row["company"], "fiscal_code")),
		}
		candidates.discard(None)
		if recipient_identifiers & candidates:
			return frappe.get_doc("EDI Configuration", row["name"])

	return None
