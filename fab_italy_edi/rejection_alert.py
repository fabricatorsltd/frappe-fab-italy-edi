from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, format_datetime, get_url_to_form, now_datetime

from fab_italy_edi.edi_lifecycle import normalize_state
from fab_italy_edi.polling_watchdog import ALERT_DATETIME_FORMAT, get_system_manager_recipients, parse_recipients

REJECTED_STATE = "rejected"
# failed is a send that never reached SdI (provider or transport error)
ALERT_STATES = {REJECTED_STATE, "failed"}
ALERT_HEADERS = {"X-Auto-Generated": "edi-rejection-alert"}


def alert_on_rejection(document, method: str | None = None) -> None:
	"""Mail an SdI rejection or a failed send once, on the save that moves the EDI Document into it.

	Every channel and the provider polling save the EDI Document after reconciling its states, so
	this hook covers them all, and comparing with the stored version keeps a later poll or edit of
	an already rejected document quiet. A retry that is rejected again alerts again.
	"""
	if not has_become_rejected(document):
		return

	try:
		send_rejection_alert(document)
	except Exception:
		# a mail that cannot go out must never undo the receipt being recorded
		frappe.log_error(
			title=_("Unable to send the SdI rejection alert"),
			message=frappe.get_traceback(),
			reference_doctype="EDI Document",
			reference_name=document.name,
		)


def has_become_rejected(document) -> bool:
	if not is_rejected(document):
		return False

	previous = document.get_doc_before_save()
	return not (previous and is_rejected(previous))


def is_rejected(document) -> bool:
	return bool(get_states(document) & ALERT_STATES)


def get_states(document) -> set[str | None]:
	return {
		normalize_state(document.get("transmission_state")),
		normalize_state(document.get("latest_receipt_state")),
	}


def send_rejection_alert(document) -> bool:
	recipients = get_rejection_alert_recipients(document.get("company"))
	if not recipients:
		frappe.log_error(
			title=_("No recipient for the SdI rejection alert"),
			message=f"EDI Document: {document.name}",
		)
		return False

	if "helpdesk" not in frappe.get_installed_apps():
		subject, message = build_rejection_alert(document)
		frappe.sendmail(recipients=recipients, subject=subject, message=message, email_headers=ALERT_HEADERS)
		return True

	from helpdesk.helpdesk.doctype.hd_settings.helpers import branded_email, get_default_language, use_language

	with use_language(get_default_language()):
		subject, message = build_rejection_alert(document, branded=True)
		email = branded_email(message, subject)

	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		with_container=False,
		email_headers=ALERT_HEADERS,
		**email,
	)
	return True


def get_rejection_alert_recipients(company: str | None) -> list[str]:
	configured = parse_recipients(
		frappe.db.get_value("EDI Configuration", {"company": company}, "rejection_alert_recipients")
		if company
		else None
	)
	return configured or get_system_manager_recipients()


def build_rejection_alert(document, branded: bool = False) -> tuple[str, str]:
	number = get_document_number(document)
	party = cstr(document.get("party_name")).strip() or "-"
	last_error = document.get("last_error")
	rows = [
		(_("SdI identifier"), get_sdi_error_field(last_error, "identificativo_sdi") or cstr(document.get("external_submission_id")) or "-"),
		(_("Receipt type"), get_sdi_error_field(last_error, "type") or get_sdi_error_field(last_error, "receipt_type") or "-"),
		(_("Reason"), get_sdi_error_reason(last_error) or "-"),
		(_("Date"), format_datetime(now_datetime(), ALERT_DATETIME_FORMAT)),
	]
	url = get_url_to_form(document.source_doctype, document.source_name)
	if REJECTED_STATE in get_states(document):
		intro = _("SdI rejected an e-invoice, it has to be corrected and sent again.")
		subject = _("[SdI rejected] {0} - {1}").format(number, party)
	else:
		intro = _("An e-invoice could not be sent to SdI, check the error and send it again.")
		subject = _("[SdI send failed] {0} - {1}").format(number, party)
	title = f"{number} - {party}"

	if not branded:
		lines = [intro, "", title, *(f"{label}: {value}" for label, value in rows), ""]
		link = f'<a href="{escape_html(url)}">{escape_html(_("Open the document"))}</a>'
		return subject, "<br>".join(escape_html(line) for line in lines) + link

	from helpdesk.helpdesk.doctype.hd_settings.helpers import DETAIL_LABEL, DETAIL_VALUE, mail_styles

	details = "\n".join(
		f'  <tr><td style="{DETAIL_LABEL}">{escape_html(label)}</td><td style="{DETAIL_VALUE}">{escape_html(value)}</td></tr>'
		for label, value in rows
	)
	message = f"""\
<p style="margin:0 0 8px;">{escape_html(intro)}</p>
<p style="margin:0 0 20px;font-size:18px;font-weight:600;line-height:1.4;">{escape_html(title)}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px;border-collapse:collapse;font-size:14px;">
{details}
</table>
<p style="margin:0;"><a href="{escape_html(url)}" style="{mail_styles().button}">{escape_html(_("Open the document"))}</a></p>
"""
	return subject, message


def get_document_number(document) -> str:
	return cstr(document.get("autofattura_document_number")).strip() or cstr(document.get("source_name"))


def get_sdi_error_reason(text: str | None) -> str:
	"""The SdI receipts reach last_error as "key: value" lines, and only the error ones say why."""
	lines = [line.strip() for line in cstr(text).splitlines() if line.strip()]
	errors = [value for key, value in map(split_field, lines) if key == "error" and value]
	if errors:
		return "; ".join(errors)

	return lines[0] if lines else ""


def get_sdi_error_field(text: str | None, key: str) -> str | None:
	for line in cstr(text).splitlines():
		name, value = split_field(line)
		if name == key and value:
			return value

	return None


def split_field(line: str) -> tuple[str | None, str]:
	name, separator, value = line.partition(":")
	if not separator:
		return None, line.strip()

	return name.strip().lower(), value.strip()
