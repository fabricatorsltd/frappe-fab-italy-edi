from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cstr
from frappe.utils.file_manager import save_file

from fab_italy_edi.sales_invoice_edi import build_status_update_message

TRANSMISSION_STATE_PROGRESS = {
	"draft": 0,
	"ready": 1,
	"queued": 2,
	"sending": 3,
	"sent": 4,
	"delivered": 5,
	"accepted": 6,
	"rejected": 6,
	"failed": 6,
	"cancelled": 6,
	"imported": 6,
}
RECEIPT_STATE_PROGRESS = {
	"unknown_pending": 0,
	"queued": 1,
	"sent": 2,
	"delivered": 3,
	"accepted": 4,
	"rejected": 4,
	"failed": 4,
	"cancelled": 4,
}
REOPENABLE_TRANSMISSION_TRANSITIONS = {
	("failed", "queued"),
	("failed", "sending"),
	("rejected", "queued"),
	("rejected", "sending"),
	("cancelled", "queued"),
	("cancelled", "sending"),
	("sending", "queued"),
}
UNSET = object()


def reconcile_edi_document_states(
	document,
	*,
	transmission_state: str | None = None,
	receipt_state: str | None = None,
	external_submission_id: str | None = None,
	canonical_identifier: str | None = None,
	last_error: str | None | object = UNSET,
	event_label: str | None = None,
	detail_text: Any = None,
	always_log: bool = False,
) -> tuple[bool, str | None]:
	previous_transmission_state = normalize_state(getattr(document, "transmission_state", None))
	previous_receipt_state = normalize_state(getattr(document, "latest_receipt_state", None))
	changed = False

	if external_submission_id and getattr(document, "external_submission_id", None) != external_submission_id:
		document.external_submission_id = external_submission_id
		changed = True

	if canonical_identifier and getattr(document, "canonical_identifier", None) != canonical_identifier:
		document.canonical_identifier = canonical_identifier
		changed = True

	normalized_transmission_state = normalize_state(transmission_state)
	if should_apply_state_transition(
		previous_transmission_state,
		normalized_transmission_state,
		progress_order=TRANSMISSION_STATE_PROGRESS,
		reopenable_transitions=REOPENABLE_TRANSMISSION_TRANSITIONS,
	):
		document.transmission_state = normalized_transmission_state
		changed = True

	normalized_receipt_state = normalize_state(receipt_state)
	if should_apply_state_transition(
		previous_receipt_state,
		normalized_receipt_state,
		progress_order=RECEIPT_STATE_PROGRESS,
	):
		document.latest_receipt_state = normalized_receipt_state
		changed = True

	if last_error is not UNSET and getattr(document, "last_error", None) != last_error:
		document.last_error = last_error
		changed = True

	activity_message = None
	if event_label:
		activity_message = build_status_update_message(
			event_label=event_label,
			previous_transmission_state=previous_transmission_state,
			transmission_state=getattr(document, "transmission_state", None),
			previous_receipt_state=previous_receipt_state,
			receipt_state=getattr(document, "latest_receipt_state", None),
			external_submission_id=getattr(document, "external_submission_id", None),
			detail_text=detail_text,
			always_log=always_log,
		)

	return changed, activity_message


def should_apply_state_transition(
	current_state: str | None,
	target_state: str | None,
	*,
	progress_order: dict[str, int],
	reopenable_transitions: set[tuple[str, str]] | None = None,
) -> bool:
	current = normalize_state(current_state)
	target = normalize_state(target_state)
	if not target or current == target:
		return False

	if not current:
		return True

	if reopenable_transitions and (current, target) in reopenable_transitions:
		return True

	current_rank = progress_order.get(current)
	target_rank = progress_order.get(target)
	if current_rank is None or target_rank is None:
		return True

	return target_rank > current_rank


def find_receipt_row(document, external_message_id: str | None, *, receipt_type: str | None = None):
	if not external_message_id:
		return None

	for row in getattr(document, "receipts", None) or []:
		if row.external_message_id != external_message_id:
			continue
		if receipt_type and row.receipt_type != receipt_type:
			continue
		return row

	return None


def append_edi_receipt(
	document,
	*,
	receipt_type: str,
	normalized_state: str | None,
	external_message_id: str,
	processing_notes: str | None = None,
	payload_file: str | None = None,
	matched: int = 1,
) -> bool:
	if find_receipt_row(document, external_message_id, receipt_type=receipt_type):
		return False

	document.append(
		"receipts",
		{
			"receipt_type": receipt_type,
			"normalized_state": normalized_state,
			"matched": matched,
			"external_message_id": external_message_id,
			"payload_file": payload_file,
			"processing_notes": processing_notes,
		},
	)
	return True


def build_receipt_message_id(*parts: Any) -> str:
	return ":".join(normalize_identifier_part(part) for part in parts)


def normalize_identifier_part(value: Any) -> str:
	text = cstr(value).strip().lower().replace(" ", "_")
	return text or "unknown"


def normalize_state(value: Any) -> str | None:
	text = cstr(value).strip().lower()
	return text or None


def is_xml_payload(payload: Any) -> bool:
	return isinstance(payload, str) and payload.lstrip().startswith("<")


def persist_payload_artifact(document, filename_prefix: str, payload: Any, fieldname: str | None = None) -> str:
	if isinstance(payload, (dict, list)):
		content = json.dumps(payload, indent=2, ensure_ascii=False)
		extension = "json"
	elif is_xml_payload(payload):
		content = str(payload)
		extension = "xml"
	else:
		content = str(payload)
		extension = "txt"

	# the outbound send runs as the roleless EDI automation user; the rest of
	# the lifecycle already writes with ignore_permissions, save_file is the
	# only call that still checks access on the attached document
	previous_flag = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True
	try:
		file_doc = save_file(
			f"{filename_prefix}.{extension}",
			content.encode("utf-8"),
			document.doctype,
			document.name,
			is_private=1,
			df=fieldname,
		)
	finally:
		frappe.flags.ignore_permissions = previous_flag
	return file_doc.file_url
