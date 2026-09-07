from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	escape_html,
	format_datetime,
	format_duration,
	get_datetime,
	now_datetime,
	validate_email_address,
)

from fab_italy_edi.backends import get_provider_adapter
from fab_italy_edi.polling import POLLING_JOB_DEFINITIONS, PollingJobDefinition, get_polling_handler

# A job is reported only after two consecutive missed cycles, so a single skipped run or a slow
# provider never raises an alarm, and the floor keeps a short interval from alerting on a hiccup.
# Above an eight hour interval the ceiling wins: waiting for the second missed cycle there would
# hide a stall for two days, which is the delay this watchdog exists to remove.
STALE_INTERVAL_FACTOR = 3
MINIMUM_STALE_MINUTES = 60
MAXIMUM_STALE_MINUTES = 24 * 60
ALERT_INTERVAL_HOURS = 24
ERROR_LOG_LOOKBACK_HOURS = 24
# The alert is read outside the desk, so the timestamps do not follow the reader's date format.
ALERT_DATETIME_FORMAT = "yyyy-MM-dd HH:mm"

PLACEHOLDER_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")
PLACEHOLDER_EMAIL_SUFFIXES = (".invalid", ".test", ".example", ".localhost")


def check_polling_health() -> None:
	"""Report the polling jobs that stopped refreshing their last run timestamp.

	Nothing may reach the scheduler from here: a watchdog that breaks the way the polling broke
	would be another silent failure. Disabled configurations are walked too, so one that was
	alerted about and then switched off does not keep a flag that would fire a false recovery.
	"""
	try:
		configuration_names = frappe.get_all("EDI Configuration", pluck="name")
	except Exception:
		log_watchdog_failure()
		return

	for configuration_name in configuration_names:
		try:
			review_polling_health(configuration_name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			log_watchdog_failure(configuration_name)


def review_polling_health(configuration_name: str, now: datetime | None = None) -> list[dict[str, Any]]:
	current_time = now or now_datetime()
	configuration = frappe.get_doc("EDI Configuration", configuration_name)
	watched_jobs = get_watched_polling_jobs(configuration)
	stale_jobs = get_stale_polling_jobs(configuration, watched_jobs, current_time)

	if not stale_jobs:
		if getattr(configuration, "polling_alert_active", 0):
			# A job that was switched off has not recovered, it stopped being ours to watch, so
			# only jobs still under watch earn the recovery mail.
			if watched_jobs:
				send_polling_recovery_alert(configuration)
			set_watchdog_state(configuration_name, {"polling_alert_active": 0})
		return []

	if not is_polling_alert_due(configuration, current_time):
		return stale_jobs

	delivered = send_polling_stall_alert(configuration, stale_jobs, current_time)
	# The attempt is recorded even when it failed, so a site that cannot send mail writes one
	# Error Log a day instead of the hourly pile this watchdog exists to replace.
	set_watchdog_state(
		configuration_name,
		{"last_polling_alert_sent_at": current_time, "polling_alert_active": int(delivered)},
	)

	return stale_jobs


def get_watched_polling_jobs(configuration) -> list[PollingJobDefinition]:
	"""Return the jobs this app would actually dispatch for the configuration.

	The checks mirror the early returns of run_polling_job, because a job that is deliberately
	never dispatched must be reported neither as broken nor as recovered.
	"""
	if not getattr(configuration, "enabled", 0):
		return []

	provider_name = getattr(configuration, "default_provider", None)
	if not provider_name:
		return []

	# The row, not the document: a dangling link then leaves the jobs unwatched instead of raising
	# once an hour.
	provider = frappe.db.get_value(
		"EDI Provider", provider_name, ["enabled", "use_status_polling", "adapter_key"], as_dict=True
	)
	if not provider or not provider.enabled:
		return []

	adapter = get_provider_adapter(provider)
	if not adapter:
		return []

	return [
		job for job in POLLING_JOB_DEFINITIONS if is_polling_job_watched(configuration, provider, adapter, job)
	]


def get_stale_polling_jobs(
	configuration, watched_jobs: list[PollingJobDefinition], now: datetime
) -> list[dict[str, Any]]:
	stale_jobs: list[dict[str, Any]] = []
	for job in watched_jobs:
		interval_minutes = cint(getattr(configuration, job.interval_field, 0))
		last_run = getattr(configuration, job.last_run_field, None)
		# A job that never ran is measured from the configuration itself, which is the earliest
		# moment the scheduler could have dispatched it.
		reference = get_datetime(last_run or configuration.modified)
		minutes_since_run = int((now - reference).total_seconds() // 60)
		if minutes_since_run < get_stale_threshold_minutes(interval_minutes):
			continue

		stale_jobs.append(
			{
				"job": job,
				"last_run": get_datetime(last_run) if last_run else None,
				"interval_minutes": interval_minutes,
				"overdue_minutes": minutes_since_run - interval_minutes,
			}
		)

	return stale_jobs


def is_polling_job_watched(configuration, provider, adapter, job: PollingJobDefinition) -> bool:
	if not getattr(configuration, job.enabled_field, 0):
		return False

	if cint(getattr(configuration, job.interval_field, 0)) <= 0:
		return False

	if job.job_key in {"outbound_status", "notification"} and not provider.use_status_polling:
		return False

	return callable(get_polling_handler(job.job_key, adapter))


def get_stale_threshold_minutes(interval_minutes: int) -> int:
	threshold = max(interval_minutes * STALE_INTERVAL_FACTOR, MINIMUM_STALE_MINUTES)
	# The last term keeps the ceiling from reporting a job before it is even due again, which the
	# doctype makes reachable by accepting an interval of a full day.
	return max(min(threshold, MAXIMUM_STALE_MINUTES), interval_minutes + MINIMUM_STALE_MINUTES)


def is_polling_alert_due(configuration, now: datetime) -> bool:
	last_alert = getattr(configuration, "last_polling_alert_sent_at", None)
	if not last_alert:
		return True

	next_due_at = add_to_date(get_datetime(last_alert), hours=ALERT_INTERVAL_HOURS, as_datetime=True)
	return now >= next_due_at


def send_polling_stall_alert(configuration, stale_jobs: list[dict[str, Any]], now: datetime) -> bool:
	subject = _("E-invoicing polling stalled for {0}").format(configuration.company)
	message = build_stall_alert_message(configuration, stale_jobs, get_recent_related_error_log(now))
	return send_polling_alert(configuration, subject, message)


def send_polling_recovery_alert(configuration) -> bool:
	subject = _("E-invoicing polling recovered for {0}").format(configuration.company)
	body = _("{0}: automatic e-invoicing polling is running again.").format(configuration.company)
	return send_polling_alert(configuration, subject, escape_html(body))


def send_polling_alert(configuration, subject: str, message: str) -> bool:
	recipients = get_polling_alert_recipients(configuration)
	if not recipients:
		frappe.log_error(
			title=_("No recipient for the e-invoicing polling alert"),
			message=f"EDI Configuration: {configuration.name}",
		)
		return False

	try:
		frappe.sendmail(recipients=recipients, subject=subject, message=message)
	except Exception:
		log_watchdog_failure(configuration.name)
		return False

	return True


def build_stall_alert_message(configuration, stale_jobs: list[dict[str, Any]], error_log) -> str:
	lines = [
		_("{0}: automatic e-invoicing polling has stalled.").format(configuration.company),
		"",
	]

	for entry in stale_jobs:
		lines.append(
			_("{0}: last run {1}, {2} behind schedule, interval {3} minutes.").format(
				get_polling_job_label(entry["job"].job_key),
				format_datetime(entry["last_run"], ALERT_DATETIME_FORMAT) if entry["last_run"] else _("never"),
				format_duration(entry["overdue_minutes"] * 60),
				entry["interval_minutes"],
			)
		)

	if error_log:
		lines.append("")
		lines.append(
			_("Most recent related Error Log: {0} ({1}).").format(
				error_log["method"], format_datetime(error_log["creation"], ALERT_DATETIME_FORMAT)
			)
		)

	lines.append("")
	lines.append(_("No further alert is sent for this configuration in the next 24 hours."))

	return "<br>".join(escape_html(line) if line else "" for line in lines)


def get_polling_job_label(job_key: str) -> str:
	labels = {
		"outbound_status": _("Outbound status polling"),
		"incoming_supplier_invoice": _("Incoming supplier invoice polling"),
		"notification": _("Notification polling"),
	}
	return labels.get(job_key, job_key)


def get_recent_related_error_log(now: datetime):
	# The creation bound keeps this off a full scan of a table that grows with every failure.
	cutoff = add_to_date(now, hours=-ERROR_LOG_LOOKBACK_HOURS, as_datetime=True)
	rows = frappe.get_all(
		"Error Log",
		filters={"creation": [">=", cutoff], "error": ["like", "%fab_italy_edi%"]},
		fields=["method", "creation"],
		order_by="creation desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def get_polling_alert_recipients(configuration) -> list[str]:
	configured = parse_recipients(getattr(configuration, "polling_alert_recipients", None))
	if configured:
		return configured

	return get_system_manager_recipients()


def parse_recipients(value: str | None) -> list[str]:
	if not value:
		return []

	recipients: list[str] = []
	for candidate in value.replace(";", ",").replace("\n", ",").split(","):
		add_recipient(recipients, candidate.strip())

	return recipients


def get_system_manager_recipients() -> list[str]:
	# frappe.utils.user.get_users_with_role drops Administrator unconditionally, and an
	# Administrator carrying a real address is exactly who should read this.
	user_names = frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": "System Manager"},
		pluck="parent",
	)
	if not user_names:
		return []

	users = frappe.get_all(
		"User",
		filters={"name": ["in", user_names], "enabled": 1, "user_type": "System User"},
		fields=["name", "email"],
	)

	recipients: list[str] = []
	for user in users:
		add_recipient(recipients, (user.get("email") or user.get("name") or "").strip())

	return recipients


def add_recipient(recipients: list[str], address: str) -> None:
	if not is_real_email_address(address):
		return

	if any(address.casefold() == known.casefold() for known in recipients):
		return

	recipients.append(address)


def is_real_email_address(address: str | None) -> bool:
	# Rejects the placeholder addresses a site ships with, Administrator's included.
	if not address or not validate_email_address(address):
		return False

	domain = address.rsplit("@", 1)[-1].lower()
	if domain in PLACEHOLDER_EMAIL_DOMAINS:
		return False

	return not domain.endswith(PLACEHOLDER_EMAIL_SUFFIXES)


def set_watchdog_state(configuration_name: str, values: dict[str, Any]) -> None:
	frappe.db.set_value("EDI Configuration", configuration_name, values, update_modified=False)


def log_watchdog_failure(configuration_name: str | None = None) -> None:
	try:
		frappe.log_error(
			title=_("Unable to check the e-invoicing polling health"),
			message=frappe.get_traceback(),
			reference_doctype="EDI Configuration" if configuration_name else None,
			reference_name=configuration_name,
		)
	except Exception:
		# The scheduler must survive even when the Error Log itself cannot be written.
		frappe.logger("fab_italy_edi").exception("Unable to check the e-invoicing polling health")
