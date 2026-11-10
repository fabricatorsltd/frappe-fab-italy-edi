from __future__ import annotations

from datetime import date

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, fmt_money, format_date, get_last_day, get_url_to_form, getdate

from fab_italy_edi.rejection_alert import get_rejection_alert_recipients
from fab_italy_edi.sdi_digest import FALLBACK_INK, ROW_CELL, SECTION_HEADING, get_tracking_start, send_branded

DIGEST_HEADERS = {"X-Auto-Generated": "edi-payables-digest"}

OVERDUE = "overdue"
WITHIN_7 = "within_7"
WITHIN_15 = "within_15"
WITHIN_31 = "within_31"
# supplier invoices by days left to their due date, the last day of each window included
INVOICE_WINDOWS = ((WITHIN_7, 7), (WITHIN_15, 15), (WITHIN_31, 31))
F24_OVERDUE = "f24_overdue"
F24_THIS_MONTH = "f24_this_month"
SECTION_ORDER = (OVERDUE, WITHIN_7, WITHIN_15, WITHIN_31, F24_OVERDUE, F24_THIS_MONTH)

# the tax calendar entries paid with an F24 that is not loaded yet
F24_EVENT_TYPES = ("F24 Deadline", "VAT Payment")
OPEN_EVENT_STATUSES = ("Planned", "Due")


def run() -> None:
	"""Mail each company with an enabled EDI Configuration what it still has to pay.

	Weekday mornings, from the cron in hooks.py, to the same recipients as the SdI digest. A
	company with nothing due gets no mail at all.
	"""
	for company in frappe.get_all("EDI Configuration", filters={"enabled": 1}, pluck="company"):
		try:
			send_digest(company)
		except Exception:
			# roll back first, an Error Log written inside the failed transaction would go with it
			frappe.db.rollback()
			frappe.log_error(
				title=_("Unable to send the payables digest"),
				message=frappe.get_traceback(),
				reference_doctype="EDI Configuration",
				reference_name=company,
			)
		frappe.db.commit()  # nosemgrep


def send_digest(company: str, today: date | None = None) -> bool:
	today = getdate(today)
	sections = [
		*bucket_invoices(get_invoice_rows(company), today),
		*bucket_f24(get_f24_rows(company), today),
	]
	if not sections:
		return False

	recipients = get_rejection_alert_recipients(company)
	if not recipients:
		frappe.log_error(title=_("No recipient for the payables digest"), message=f"Company: {company}")
		return False

	send_branded(recipients, lambda ink: build_digest(sections, today, ink=ink), DIGEST_HEADERS)
	return True


def get_invoice_rows(company: str) -> list[frappe._dict]:
	"""Supplier invoices with something left to pay, from the start of the SdI tracking: the older
	ones came from Odoo with their payments unreconciled.

	Drafts are listed too, at their full amount: an invoice received from SdI stays a draft until
	it is booked, and it falls due all the same.
	"""
	fields = [
		"name",
		"docstatus",
		"supplier",
		"supplier_name",
		"bill_no",
		"posting_date",
		"due_date",
		"grand_total",
		"outstanding_amount",
		"currency",
		"party_account_currency",
	]
	common = {"company": company, "is_return": 0, "posting_date": [">=", get_tracking_start(company)]}
	submitted = frappe.get_all(
		"Purchase Invoice", filters={**common, "docstatus": 1, "outstanding_amount": [">", 0]}, fields=fields
	)
	drafts = frappe.get_all("Purchase Invoice", filters={**common, "docstatus": 0, "grand_total": [">", 0]}, fields=fields)
	# SdI hands back our own autofatture as inbound invoices: their supplier invoice is already listed
	own = set(frappe.get_all("Autofattura", filters={"company": company}, pluck="document_number")) if drafts else set()

	rows = []
	for invoice in [*submitted, *drafts]:
		draft = invoice.docstatus == 0
		if draft and invoice.bill_no in own:
			continue
		detail = _("Invoice {0}").format(invoice.bill_no) if invoice.bill_no else _("Invoice")
		rows.append(
			frappe._dict(
				doctype="Purchase Invoice",
				name=invoice.name,
				number=invoice.name,
				party=invoice.supplier_name or invoice.supplier,
				detail=f"{detail} \u00b7 {_('Draft')}" if draft else detail,
				amount=(
					fmt_money(invoice.grand_total, currency=invoice.currency)
					if draft
					else fmt_money(invoice.outstanding_amount, currency=invoice.party_account_currency)
				),
				deadline=getdate(invoice.due_date or invoice.posting_date),
			)
		)
	return rows


def get_f24_rows(company: str) -> list[frappe._dict]:
	"""F24 loaded and still in draft, which is paid once submitted, and the open tax calendar
	deadlines no draft F24 covers yet."""
	rows = []
	covered = set()
	if frappe.db.exists("DocType", "F24 Payment"):
		for payment in frappe.get_all(
			"F24 Payment",
			filters={"docstatus": 0, "company": company, "data_versamento": ["is", "set"]},
			fields=["name", "data_versamento", "saldo_finale", "tax_calendar_event"],
		):
			covered.add(payment.tax_calendar_event)
			rows.append(
				frappe._dict(
					doctype="F24 Payment",
					name=payment.name,
					number=payment.name,
					party="",
					detail="F24",
					amount=fmt_money(payment.saldo_finale, currency="EUR"),
					deadline=getdate(payment.data_versamento),
				)
			)

	if frappe.db.exists("DocType", "Tax Calendar Event"):
		for event in frappe.get_all(
			"Tax Calendar Event",
			filters={
				"company": company,
				"event_type": ["in", F24_EVENT_TYPES],
				"status": ["in", OPEN_EVENT_STATUSES],
				"direction": "Outflow",
				"event_date": ["is", "set"],
			},
			fields=["name", "event_type", "event_date", "amount", "notes"],
		):
			if event.name in covered:
				continue
			rows.append(
				frappe._dict(
					doctype="Tax Calendar Event",
					name=event.name,
					number=_(event.event_type),
					party=cstr(event.notes).strip(),
					detail=_("Tax calendar"),
					amount=fmt_money(event.amount, currency="EUR"),
					deadline=getdate(event.event_date),
				)
			)

	return rows


def bucket_invoices(rows: list[frappe._dict], today: date) -> list[tuple[str, list[frappe._dict]]]:
	"""Overdue, then due within 7, 15 and 31 days; anything later waits for a later mail."""
	buckets = {key: [] for key, _days in ((OVERDUE, 0), *INVOICE_WINDOWS)}
	for row in rows:
		left = (row.deadline - today).days
		if left < 0:
			buckets[OVERDUE].append(row)
			continue
		key = next((key for key, days in INVOICE_WINDOWS if left <= days), None)
		if key:
			buckets[key].append(row)
	return sorted_sections(buckets)


def bucket_f24(rows: list[frappe._dict], today: date) -> list[tuple[str, list[frappe._dict]]]:
	"""Overdue, then due by the end of this month."""
	month_end = getdate(get_last_day(today))
	buckets = {F24_OVERDUE: [], F24_THIS_MONTH: []}
	for row in rows:
		if row.deadline < today:
			buckets[F24_OVERDUE].append(row)
		elif row.deadline <= month_end:
			buckets[F24_THIS_MONTH].append(row)
	return sorted_sections(buckets)


def sorted_sections(buckets: dict) -> list[tuple[str, list[frappe._dict]]]:
	for rows in buckets.values():
		rows.sort(key=lambda row: (row.deadline, row.number))
	return [(key, buckets[key]) for key in SECTION_ORDER if buckets.get(key)]


def build_digest(sections, today: date, ink: str = FALLBACK_INK) -> tuple[str, str]:
	headings = {
		OVERDUE: _("Overdue supplier invoices"),
		WITHIN_7: _("Supplier invoices due within 7 days"),
		WITHIN_15: _("Supplier invoices due in 8 to 15 days"),
		WITHIN_31: _("Supplier invoices due in 16 to 31 days"),
		F24_OVERDUE: _("Overdue F24"),
		F24_THIS_MONTH: _("F24 due this month"),
	}
	subject = _("[Payments due] {0}").format(format_date(today))
	parts = [f'<p style="margin:0 0 8px;">{escape_html(_("Supplier invoices and F24 still to pay."))}</p>']
	for key, rows in sections:
		parts.append(f'<p style="{SECTION_HEADING}">{escape_html(headings[key])} ({len(rows)})</p>')
		body = "\n".join(payment_row(row, today, ink) for row in rows)
		parts.append(
			f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
{body}
</table>"""
		)
	return subject, "\n".join(parts) + "\n"


def payment_row(row, today: date, ink: str) -> str:
	"""Number and party over what it is and the amount, the due date on the right."""
	url = get_url_to_form(row.doctype, row.name)
	party = f" {escape_html(row.party)}" if row.party else ""
	return f"""  <tr>
    <td style="{ROW_CELL}"><a href="{escape_html(url)}" style="color:{ink};font-weight:600;text-decoration:none;">{escape_html(cstr(row.number))}</a>{party}<br><span style="font-size:13px;color:#6b7280;">{escape_html(row.detail)} &middot; {escape_html(row.amount)}</span></td>
    <td style="{ROW_CELL}padding-left:12px;text-align:right;white-space:nowrap;font-size:13px;color:#6b7280;">{escape_html(deadline_label(row, today))}</td>
  </tr>"""


def deadline_label(row, today: date) -> str:
	day = row.deadline.strftime("%d/%m")
	if row.deadline < today:
		return _("overdue since {0}").format(day)
	return _("due {0}").format(day)
