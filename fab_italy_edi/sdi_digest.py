from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cstr, escape_html, fmt_money, format_date, get_first_day, get_url_to_form, getdate

from fab_italy_edi.autofattura import looks_like_foreign_supplier, resolve_supplier_country
from fab_italy_edi.edi_lifecycle import normalize_state
from fab_italy_edi.rejection_alert import ALERT_STATES, REJECTED_STATE, get_rejection_alert_recipients, get_sdi_error_reason

# not yet handed to SdI; every later state counts as sent
UNSENT_STATES = {None, "draft", "ready"}
SENT_STATES = {"queued", "sending", "sent", "delivered", "accepted"}
IN_FLIGHT_STATES = {"queued", "sending"}
# Deadlines kept simple: an immediate invoice goes out within 12 days of its date, an
# autofattura TD17/TD18/TD19 by the 15th of the month after the supplier invoice date.
SALES_INVOICE_DAYS = 12
AUTOFATTURA_DAY = 15
# the mail goes out on weekdays only, so "soon" counts working days
SOON_WORKING_DAYS = 2
WEEKEND = (5, 6)
DIGEST_HEADERS = {"X-Auto-Generated": "edi-sdi-digest"}
FALLBACK_INK = "#1c1917"
SECTION_HEADING = "margin:28px 0 6px;font-size:13px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:#6b7280;"
ROW_CELL = "padding:10px 0;border-top:1px solid #e5e7eb;vertical-align:top;"

REJECTED = "rejected"
OVERDUE = "overdue"
SOON = "soon"
LATER = "later"
SECTION_ORDER = (REJECTED, OVERDUE, SOON, LATER)

# translated when the mail is rendered, in the language of the mail
INVOICE = "invoice"
CREDIT_NOTE = "credit_note"
AUTOFATTURA = "autofattura"
AUTOFATTURA_TO_PREPARE = "autofattura_to_prepare"


def run() -> None:
	"""Mail each company with an enabled EDI Configuration the documents still to send to SdI.

	Weekday mornings, from the cron in hooks.py. A company with nothing to send gets no mail at
	all. There is no once a day mark: a manual rerun sends the same digest again, which is harmless.
	"""
	for company in frappe.get_all("EDI Configuration", filters={"enabled": 1}, pluck="company"):
		try:
			send_digest(company)
		except Exception:
			# roll back first, an Error Log written inside the failed transaction would go with it
			frappe.db.rollback()
			frappe.log_error(
				title=_("Unable to send the SdI digest"),
				message=frappe.get_traceback(),
				reference_doctype="EDI Configuration",
				reference_name=company,
			)
		frappe.db.commit()  # nosemgrep


def send_digest(company: str, today: date | None = None) -> bool:
	today = getdate(today)
	sections = bucket_rows(collect_rows(company), today)
	if not sections:
		return False

	recipients = get_rejection_alert_recipients(company)
	if not recipients:
		frappe.log_error(title=_("No recipient for the SdI digest"), message=f"Company: {company}")
		return False

	send_branded(recipients, lambda ink: build_digest(sections, today, ink=ink), DIGEST_HEADERS)
	return True


def send_branded(recipients: list[str], build, headers: dict) -> None:
	"""Mail the subject and message `build(ink)` returns, in the helpdesk branded page when the
	helpdesk is installed and in the site language."""
	if "helpdesk" not in frappe.get_installed_apps():
		subject, message = build(FALLBACK_INK)
		frappe.sendmail(recipients=recipients, subject=subject, message=message, email_headers=headers)
		return

	from helpdesk.helpdesk.doctype.hd_settings.helpers import branded_email, get_default_language, mail_styles, use_language

	with use_language(get_default_language()):
		subject, message = build(mail_styles().ink)
		email = branded_email(message, subject)

	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		with_container=False,
		email_headers=headers,
		**email,
	)


def collect_rows(company: str) -> list[frappe._dict]:
	start = get_tracking_start(company)
	return [
		*get_sales_invoice_rows(company, start),
		*get_autofattura_rows(company),
		*get_missing_autofattura_rows(company, start),
	]


def get_tracking_start(company: str) -> date:
	"""Older documents predate the e-invoicing setup (the Odoo import) and are never listed."""
	start, created = frappe.db.get_value("EDI Configuration", company, ["sdi_digest_start_date", "creation"])
	return getdate(start or created)


def get_sales_invoice_rows(company: str, start: date) -> list[frappe._dict]:
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": 1, "company": company},
		# older invoices only matter as unresolved rejections, which always have an EDI Document
		or_filters={"posting_date": [">=", start], "fab_edi_document": ["is", "set"]},
		fields=[
			"name",
			"customer",
			"customer_name",
			"posting_date",
			"is_return",
			"grand_total",
			"currency",
			"fab_edi_transmission_state",
			"fab_edi_receipt_state",
			"fab_edi_document",
		],
		order_by="posting_date asc, name asc",
	)
	rows = []
	for invoice in invoices:
		status = get_status(invoice.fab_edi_transmission_state, invoice.fab_edi_receipt_state)
		if not status:
			continue
		# A rejection stays listed until it is sent again, however old the invoice. The ones
		# imported from Odoo carry their old states but no EDI Document: that history is closed.
		if status == REJECTED and not invoice.fab_edi_document:
			continue
		if status != REJECTED and getdate(invoice.posting_date) < start:
			continue

		rows.append(
			frappe._dict(
				doctype="Sales Invoice",
				name=invoice.name,
				number=invoice.name,
				party=invoice.customer_name or invoice.customer,
				kind=CREDIT_NOTE if invoice.is_return else INVOICE,
				amount=fmt_money(invoice.grand_total, currency=invoice.currency),
				deadline=sales_invoice_deadline(invoice.posting_date),
				status=status,
				failed=is_failed(invoice.fab_edi_transmission_state, invoice.fab_edi_receipt_state),
				reason=get_reason(status, edi_document=invoice.fab_edi_document),
			)
		)

	return rows


def get_autofattura_rows(company: str) -> list[frappe._dict]:
	"""Every autofattura still to send, whatever its date: it was prepared on purpose."""
	autofatture = frappe.get_all(
		"Autofattura",
		filters={"company": company},
		fields=[
			"name",
			"document_number",
			"document_type",
			"supplier",
			"supplier_name",
			"supplier_invoice_date",
			"source_purchase_invoice",
			"grand_total",
			"currency",
			"transmission_state",
			"latest_receipt_state",
			"linked_edi_document",
			"last_error",
		],
		order_by="name asc",
	)
	rows = []
	for autofattura in autofatture:
		status = get_status(autofattura.transmission_state, autofattura.latest_receipt_state)
		if not status:
			continue

		source = (
			frappe.db.get_value(
				"Purchase Invoice",
				autofattura.source_purchase_invoice,
				["docstatus", "bill_date", "posting_date"],
				as_dict=True,
			)
			if autofattura.source_purchase_invoice
			else None
		)
		# the purchase invoice was cancelled, its autofattura is no longer due
		if source and source.docstatus == 2:
			continue

		supplier_date = autofattura.supplier_invoice_date or (source and (source.bill_date or source.posting_date))
		rows.append(
			frappe._dict(
				doctype="Autofattura",
				name=autofattura.name,
				number=autofattura.document_number or autofattura.name,
				party=autofattura.supplier_name or autofattura.supplier,
				kind=AUTOFATTURA,
				document_type=autofattura.document_type,
				amount=fmt_money(autofattura.grand_total, currency=autofattura.currency),
				deadline=autofattura_deadline(supplier_date) if supplier_date else None,
				status=status,
				failed=is_failed(autofattura.transmission_state, autofattura.latest_receipt_state),
				reason=get_reason(status, last_error=autofattura.last_error, edi_document=autofattura.linked_edi_document),
			)
		)

	return rows


def get_missing_autofattura_rows(company: str, start: date) -> list[frappe._dict]:
	"""Foreign supplier invoices that need an autofattura and have none yet.

	Returns are left out: a credit note from a foreign supplier is settled on the autofattura of
	the invoice it corrects, it does not get one of its own here.
	"""
	invoices = frappe.db.sql(
		"""
			SELECT pi.name, pi.supplier, pi.supplier_name, pi.bill_date, pi.posting_date,
				pi.grand_total, pi.currency
			FROM `tabPurchase Invoice` pi
			WHERE pi.docstatus = 1 AND pi.is_return = 0 AND pi.company = %(company)s
				AND pi.posting_date >= %(start)s
				AND IFNULL(pi.fab_edi_autofattura, '') = ''
				AND NOT EXISTS (
					SELECT 1 FROM `tabAutofattura` a WHERE a.source_purchase_invoice = pi.name
				)
			ORDER BY pi.posting_date ASC, pi.name ASC
		""",
		{"company": company, "start": start},
		as_dict=True,
	)
	foreign = {}
	rows = []
	for invoice in invoices:
		if invoice.supplier not in foreign:
			foreign[invoice.supplier] = is_foreign_supplier(invoice.supplier)
		if not foreign[invoice.supplier]:
			continue

		rows.append(
			frappe._dict(
				doctype="Purchase Invoice",
				name=invoice.name,
				number=invoice.name,
				party=invoice.supplier_name or invoice.supplier,
				kind=AUTOFATTURA_TO_PREPARE,
				amount=fmt_money(invoice.grand_total, currency=invoice.currency),
				deadline=autofattura_deadline(invoice.bill_date or invoice.posting_date),
				status=LATER,
				failed=False,
				reason="",
			)
		)

	return rows


def is_foreign_supplier(supplier: str | None) -> bool:
	if not supplier:
		return False

	tax_id = frappe.get_cached_value("Supplier", supplier, "tax_id")
	return looks_like_foreign_supplier(resolve_supplier_country(supplier), tax_id)


def get_status(transmission_state: str | None, receipt_state: str | None) -> str | None:
	"""REJECTED, LATER for a document still to send, None for one sent or cancelled.

	LATER only marks it as unsent, bucket_rows() places it by its deadline.
	"""
	transmission = normalize_state(transmission_state)
	# sent again after a rejection: the old receipt stays until SdI answers the new send
	if transmission in IN_FLIGHT_STATES:
		return None
	states = {transmission, normalize_state(receipt_state)}
	if states & ALERT_STATES:
		return REJECTED
	if transmission in UNSENT_STATES and not states & SENT_STATES:
		return LATER
	return None


def is_failed(transmission_state: str | None, receipt_state: str | None) -> bool:
	states = {normalize_state(transmission_state), normalize_state(receipt_state)}
	return REJECTED_STATE not in states


def get_reason(status: str, last_error: str | None = None, edi_document: str | None = None) -> str:
	if status != REJECTED:
		return ""
	if not last_error and edi_document:
		last_error = frappe.db.get_value("EDI Document", edi_document, "last_error")
	return get_sdi_error_reason(last_error)


def sales_invoice_deadline(posting_date) -> date:
	return getdate(add_days(posting_date, SALES_INVOICE_DAYS))


def autofattura_deadline(supplier_invoice_date) -> date:
	return getdate(add_months(get_first_day(supplier_invoice_date), 1)).replace(day=AUTOFATTURA_DAY)


def soon_horizon(today: date) -> date:
	"""The last day of the next SOON_WORKING_DAYS working days: on a Friday, the Tuesday."""
	day, left = today, SOON_WORKING_DAYS
	while left:
		day += timedelta(days=1)
		if day.weekday() not in WEEKEND:
			left -= 1
	return day


def bucket_rows(rows: list[frappe._dict], today: date) -> list[tuple[str, list[frappe._dict]]]:
	"""The non empty sections in mail order, each row in exactly one, soonest deadline first."""
	horizon = soon_horizon(today)
	buckets = {key: [] for key in SECTION_ORDER}
	for row in rows:
		if row.status == REJECTED:
			buckets[REJECTED].append(row)
		elif row.deadline and row.deadline < today:
			buckets[OVERDUE].append(row)
		elif row.deadline and row.deadline <= horizon:
			buckets[SOON].append(row)
		else:
			buckets[LATER].append(row)

	buckets[REJECTED].sort(key=lambda row: row.number)
	for key in (OVERDUE, SOON, LATER):
		buckets[key].sort(key=lambda row: (row.deadline or date.max, row.number))
	return [(key, buckets[key]) for key in SECTION_ORDER if buckets[key]]


def build_digest(sections, today: date, ink: str = FALLBACK_INK) -> tuple[str, str]:
	headings = {
		REJECTED: _("Rejected by SdI"),
		OVERDUE: _("Send overdue"),
		SOON: _("To send within 2 days"),
		LATER: _("To send"),
	}
	subject = _("[Invoices to send to SdI] {0}").format(format_date(today))
	parts = [f'<p style="margin:0 0 8px;">{escape_html(_("These documents have not been sent to SdI yet."))}</p>']
	for key, rows in sections:
		parts.append(f'<p style="{SECTION_HEADING}">{escape_html(headings[key])} ({len(rows)})</p>')
		parts.append(row_table(rows, today, ink))
	return subject, "\n".join(parts) + "\n"


def row_table(rows, today: date, ink: str) -> str:
	body = "\n".join(document_row(row, today, ink) for row in rows)
	return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
{body}
</table>"""


def document_row(row, today: date, ink: str) -> str:
	"""Number and party over kind and amount, the deadline on the right; a rejection adds its reason."""
	url = get_url_to_form(row.doctype, row.name)
	details = f"{escape_html(kind_label(row))} &middot; {escape_html(row.amount)}"
	if row.status == REJECTED and row.reason:
		details += f'<br><span style="color:{ink};">{escape_html(row.reason)}</span>'
	return f"""  <tr>
    <td style="{ROW_CELL}"><a href="{escape_html(url)}" style="color:{ink};font-weight:600;text-decoration:none;">{escape_html(cstr(row.number))}</a> {escape_html(cstr(row.party))}<br><span style="font-size:13px;color:#6b7280;">{details}</span></td>
    <td style="{ROW_CELL}padding-left:12px;text-align:right;white-space:nowrap;font-size:13px;color:#6b7280;">{escape_html(deadline_label(row, today))}</td>
  </tr>"""


def kind_label(row) -> str:
	if row.kind == CREDIT_NOTE:
		return _("Credit note")
	if row.kind == AUTOFATTURA:
		return _("Autofattura {0}").format(cstr(row.document_type)).strip()
	if row.kind == AUTOFATTURA_TO_PREPARE:
		return _("Autofattura to prepare")
	return _("Invoice")


def deadline_label(row, today: date) -> str:
	if row.status == REJECTED:
		return _("Send failed") if row.failed else _("Rejected")
	if not row.deadline:
		return ""

	late = (today - row.deadline).days
	if late > 0:
		return _("overdue by 1 day") if late == 1 else _("overdue by {0} days").format(late)
	return _("due {0}").format(row.deadline.strftime("%d/%m"))
