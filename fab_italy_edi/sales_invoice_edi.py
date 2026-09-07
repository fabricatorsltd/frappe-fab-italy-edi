from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import getdate

from erpnext.controllers.accounts_controller import get_payment_terms

from fab_italy_edi.fatturapa.regional_compat import is_italian_company


ATTENTION_STATES = {"rejected", "failed", "cancelled"}


def keep_manual_due_date(document: Any, method: str | None = None) -> None:
	"""Make the Payment Terms Template and the due date override each other.

	``AccountsController.validate_invoice_documents_schedule`` reads the due date back from the
	payment schedule, rebuilds that schedule only when it is empty, and then validates the date
	against the template, so a date or a template changed on its own is either dropped or throws.
	All of it happens before the ``validate`` hook, hence the work here, on the document as it was
	sent: whichever of the two the caller moved wins, and the loser follows.
	"""
	if document.get("is_pos") or document.get("is_return"):
		return

	# terms taken from a linked order or from an import are not ours to manage, and ERPNext
	# already exempts them from its own due date checks
	if document.get("ignore_default_payment_terms_template"):
		return

	schedule = document.get("payment_schedule") or []
	if not schedule or not all(row.get("due_date") for row in schedule):
		return

	if is_schedule_as_stored(document, schedule):
		if is_payment_terms_template_replaced(document):
			# the schedule is rebuilt from the template only when there is none, so make room,
			# and let set_missing_values date the invoice off the template that just arrived
			document.set("payment_schedule", [])
			document.due_date = None
			return

		if is_due_date_typed_by_hand(document):
			schedule = apply_manual_due_date(document, schedule)

	drop_payment_terms_template_off_manual_schedule(document, schedule)


def is_schedule_as_stored(document: Any, schedule: list[Any]) -> bool:
	"""True when this save leaves the payment schedule exactly as it was stored.

	A schedule that moved is the caller's own: the form refetches it whenever the template, the
	posting date or the party change, so only an untouched one follows the header fields.
	"""
	stored = document.get_doc_before_save()
	if not stored:
		return False

	stored_due_dates = {row.name: row.due_date for row in stored.get("payment_schedule") or []}
	if len(stored_due_dates) != len(schedule):
		return False

	return all(
		stored_due_dates.get(row.name) and getdate(stored_due_dates[row.name]) == getdate(row.due_date)
		for row in schedule
	)


def is_payment_terms_template_replaced(document: Any) -> bool:
	template = document.get("payment_terms_template")
	return bool(template) and template != document.get_doc_before_save().get("payment_terms_template")


def is_due_date_typed_by_hand(document: Any) -> bool:
	stored = document.get_doc_before_save()
	if not document.get("due_date") or not stored.get("due_date"):
		return False

	return getdate(stored.due_date) != getdate(document.due_date)


def apply_manual_due_date(document: Any, schedule: list[Any]) -> list[Any]:
	"""Move the schedule row that carries the document due date, which is the last one, onto it.

	An instalment falling on or after the typed date would outlive the date it is meant to end on,
	and would trip the duplicate due date check when the two dates meet, so there the typed date
	replaces the schedule with the row that carried it, mode of payment and bank details included.
	"""
	due_date = getdate(document.due_date)
	last_row = max(schedule, key=lambda row: getdate(row.due_date))
	last_row.due_date = due_date
	clear_payment_term(last_row)

	if any(row is not last_row and getdate(row.due_date) >= due_date for row in schedule):
		last_row.idx = 1
		last_row.invoice_portion = 100
		document.set("payment_schedule", [last_row])

	return document.get("payment_schedule")


def clear_payment_term(row: Any) -> None:
	"""Take the Payment Term, and the delay it counts, off a row that no longer follows it."""
	row.payment_term = None
	row.due_date_based_on = None
	row.credit_days = 0
	row.credit_months = 0


def drop_payment_terms_template_off_manual_schedule(document: Any, schedule: list[Any]) -> None:
	"""Unlink the template, and the terms behind it, once the schedule dates are not its own.

	Only the template link is cleared: ``ignore_default_payment_terms_template`` would also skip
	the payment schedule amount validation, and the rows keep their mode of payment because that
	is what the invoice prints.
	"""
	if not document.get("payment_terms_template"):
		return

	term_due_dates = [
		getdate(term.due_date)
		for term in get_payment_terms(document.payment_terms_template, document.get("posting_date")) or []
	]
	if sorted(getdate(row.due_date) for row in schedule) == sorted(term_due_dates):
		return

	document.payment_terms_template = None
	for row in schedule:
		if getdate(row.due_date) not in term_due_dates:
			clear_payment_term(row)


def set_due_date_from_payment_schedule(document: Any, method: str | None = None) -> None:
	"""Put the document due date back on the last payment schedule row.

	``AccountsController.set_due_date`` runs before ``set_payment_schedule``, so on the save that
	first builds the schedule the due date is still whatever the document carried. Applying the
	same rule again, once the schedule is final, is what makes a template set the date.
	"""
	due_dates = [
		getdate(row.due_date) for row in document.get("payment_schedule") or [] if row.get("due_date")
	]
	if due_dates:
		document.due_date = max(due_dates)


def fill_payment_schedule_bank_account(document: Any, method: str | None = None) -> None:
	"""Put the company Bank Account on the payment schedule rows that lack one.

	``erpnext.regional.italy.utils.prepare_payment_schedule`` falls back to
	``Company.default_bank_account``, which links to Account and not to Bank Account, so
	without the Bank Account on the row the IBAN never reaches DatiPagamento.
	"""
	if not document.get("payment_schedule"):
		return

	if not is_italian_company(document.get("company")):
		return

	bank_account = get_company_bank_account(document.company)
	if not bank_account:
		return

	for row in document.payment_schedule:
		if not row.get("bank_account"):
			row.bank_account = bank_account

	# The name, IBAN and BIC columns are fetch fields that only follow a link change,
	# so rows rebuilt on submit keep them empty; write them so the stored schedule
	# carries the bank data (email templates read the row, the XML refills it anyway).
	details = {}
	for row in document.payment_schedule:
		if not row.get("bank_account"):
			continue
		if row.bank_account not in details:
			details[row.bank_account] = get_bank_account_details(row.bank_account)
		for fieldname, value in details[row.bank_account].items():
			if not row.get(fieldname) and value:
				row.set(fieldname, value)


def get_bank_account_details(bank_account: str) -> dict[str, str | None]:
	account = frappe.db.get_value(
		"Bank Account", bank_account, ["bank", "iban", "swift_number"], as_dict=True
	)
	if not account:
		return {}
	return {
		"bank_account_name": account.bank,
		"bank_account_iban": account.iban,
		"bank_account_swift_number": account.swift_number
		or frappe.db.get_value("Bank", account.bank, "swift_number"),
	}


def get_company_bank_account(company: str) -> str | None:
	"""Company Bank Account carrying an IBAN: the default one, or the only one there is."""
	bank_accounts = frappe.get_all(
		"Bank Account",
		filters={"company": company, "is_company_account": 1, "iban": ["is", "set"], "disabled": 0},
		fields=["name", "is_default"],
	)

	default_accounts = [row["name"] for row in bank_accounts if row["is_default"]]
	if len(default_accounts) == 1:
		return default_accounts[0]

	return bank_accounts[0]["name"] if len(bank_accounts) == 1 else None


def sync_sales_invoice_tracking(document: Any, *, activity_message: str | None = None) -> None:
	invoice_name = get_linked_sales_invoice_name(document)
	if not invoice_name:
		return

	sync_sales_invoice_edi_fields(invoice_name, document)
	if activity_message:
		add_sales_invoice_activity(invoice_name, activity_message)


def get_linked_sales_invoice_name(document: Any) -> str | None:
	if get_document_field(document, "source_doctype") != "Sales Invoice":
		return None
	return get_document_field(document, "source_name")


def get_document_field(document: Any, fieldname: str) -> Any:
	getter = getattr(document, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(document, fieldname, None)


def sync_sales_invoice_edi_fields(invoice_name: str, document: Any) -> None:
	frappe.db.set_value(
		"Sales Invoice",
		invoice_name,
		{
			"fab_edi_document": document.name,
			"fab_edi_transmission_state": document.transmission_state,
			"fab_edi_receipt_state": document.latest_receipt_state,
		},
		update_modified=False,
	)


def add_sales_invoice_activity(invoice_name: str, message: str) -> None:
	frappe.get_doc("Sales Invoice", invoice_name).add_comment("Info", message)


def build_send_success_message(external_submission_id: str, *, transport_label: str = "SDI") -> str:
	return f"FAB EDI queued invoice to {transport_label} as {external_submission_id}."


def build_send_failure_message(error_text: Any) -> str:
	return f"FAB EDI send to SDI failed: {first_line(error_text)}"


def build_status_update_message(
	*,
	event_label: str,
	previous_transmission_state: str | None,
	transmission_state: str | None,
	previous_receipt_state: str | None,
	receipt_state: str | None,
	external_submission_id: str | None = None,
	detail_text: Any = None,
	always_log: bool = False,
) -> str | None:
	changes: list[str] = []

	if previous_transmission_state != transmission_state and transmission_state:
		changes.append(
			f"transmission {format_edi_state(previous_transmission_state)} -> {format_edi_state(transmission_state)}"
		)

	if previous_receipt_state != receipt_state and receipt_state:
		changes.append(f"receipt {format_edi_state(previous_receipt_state)} -> {format_edi_state(receipt_state)}")

	if changes:
		summary = "; ".join(changes)
	elif always_log:
		summary = f"status {format_edi_state(receipt_state or transmission_state)}"
	else:
		return None

	parts = [f"FAB EDI {event_label}: {summary}"]
	if external_submission_id:
		parts.append(f"ID: {external_submission_id}")

	attention_state = receipt_state or transmission_state or ""
	if attention_state in ATTENTION_STATES and detail_text:
		parts.append(first_line(detail_text))

	return " · ".join(parts)


def format_edi_state(state: str | None) -> str:
	text = str(state or "unknown").replace("_", " ").strip()
	return " ".join(part.capitalize() for part in text.split()) or "Unknown"


def first_line(text: Any) -> str:
	return str(text or "").splitlines()[0].strip()
