from __future__ import annotations

from typing import Any

import frappe

from fab_italy_edi.fatturapa.regional_compat import is_italian_company


ATTENTION_STATES = {"rejected", "failed", "cancelled"}


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
