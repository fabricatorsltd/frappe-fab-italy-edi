from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import sleep
from typing import Any, Mapping

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from fab_italy_edi.automation import automation_user_context
from fab_italy_edi.backends import get_provider_adapter
from fab_italy_edi.provider_sync import sync_notification, sync_outbound_invoice, upsert_incoming_invoice_document


@dataclass(frozen=True)
class PollingJobDefinition:
	job_key: str
	enabled_field: str
	interval_field: str
	last_run_field: str
	dispatch_method: str


POLLING_JOB_DEFINITIONS = (
	PollingJobDefinition(
		job_key="outbound_status",
		enabled_field="auto_refresh_enabled",
		interval_field="outbound_status_polling_interval_mins",
		last_run_field="last_outbound_status_polling_at",
		dispatch_method="poll_outbound_statuses",
	),
	PollingJobDefinition(
		job_key="incoming_supplier_invoice",
		enabled_field="auto_refresh_enabled",
		interval_field="incoming_invoice_polling_interval_mins",
		last_run_field="last_incoming_invoice_polling_at",
		dispatch_method="fetch_incoming_supplier_invoices",
	),
	PollingJobDefinition(
		job_key="notification",
		enabled_field="auto_refresh_enabled",
		interval_field="notification_polling_interval_mins",
		last_run_field="last_notification_polling_at",
		dispatch_method="fetch_notifications",
	),
)
POLLING_JOB_MAP = {job.job_key: job for job in POLLING_JOB_DEFINITIONS}


def run_automatic_refresh_scheduler() -> None:
	current_time = now_datetime()
	fields = [
		"name",
		"company",
		"enabled",
		"default_provider",
		"auto_refresh_enabled",
		"outbound_status_polling_interval_mins",
		"incoming_invoice_polling_interval_mins",
		"notification_polling_interval_mins",
		"last_outbound_status_polling_at",
		"last_incoming_invoice_polling_at",
		"last_notification_polling_at",
	]
	configurations = frappe.get_all(
		"EDI Configuration",
		filters={"enabled": 1, "auto_refresh_enabled": 1},
		fields=fields,
	)

	for configuration in configurations:
		for job in get_due_polling_jobs(configuration, now=current_time):
			enqueue_polling_job(configuration["name"], job.job_key, current_time)


def get_due_polling_jobs(
	configuration: Mapping[str, Any], now: datetime | None = None
) -> list[PollingJobDefinition]:
	current_time = now or now_datetime()
	due_jobs: list[PollingJobDefinition] = []

	for job in POLLING_JOB_DEFINITIONS:
		if is_polling_job_due(configuration, job, current_time):
			due_jobs.append(job)

	return due_jobs


def is_polling_job_due(
	configuration: Mapping[str, Any], job: PollingJobDefinition, now: datetime
) -> bool:
	if not configuration.get(job.enabled_field):
		return False

	interval_minutes = cint(configuration.get(job.interval_field))
	if interval_minutes <= 0:
		return False

	last_run = configuration.get(job.last_run_field)
	if not last_run:
		return True

	next_due_at = add_to_date(get_datetime(last_run), minutes=interval_minutes, as_datetime=True)
	return now >= next_due_at


def enqueue_polling_job(configuration_name: str, job_key: str, current_time: datetime | None = None) -> None:
	frappe.enqueue(
		"fab_italy_edi.polling.run_polling_job",
		queue="long",
		job_name=f"fab_italy_edi:{configuration_name}:{job_key}",
		enqueue_after_commit=True,
		configuration_name=configuration_name,
		job_key=job_key,
	)


def run_polling_job(configuration_name: str, job_key: str):
	configuration = frappe.get_doc("EDI Configuration", configuration_name)
	if not configuration.enabled or not configuration.auto_refresh_enabled:
		return None

	provider_name = configuration.default_provider
	if not provider_name:
		return None

	provider = frappe.get_doc("EDI Provider", provider_name)
	if not provider.enabled:
		return None

	if job_key in {"outbound_status", "notification"} and not provider.use_status_polling:
		return None

	adapter = get_provider_adapter(provider)
	if not adapter:
		return None

	job = POLLING_JOB_MAP[job_key]
	handler = get_polling_handler(job_key, adapter)
	if not callable(handler):
		return None

	with automation_user_context():
		result = handler(configuration=configuration, provider=provider)
		update_polling_timestamp(configuration_name, job.last_run_field)
	return result


def get_polling_handler(job_key: str, adapter):
	if job_key == "outbound_status" and callable(getattr(adapter, "get_outbound_invoice", None)):
		return lambda configuration, provider: poll_outbound_statuses_with_backend(
			configuration, provider, adapter
		)
	if job_key == "notification" and callable(getattr(adapter, "iter_invoice_notifications", None)):
		return lambda configuration, provider: fetch_notifications_with_backend(
			configuration, provider, adapter
		)
	if job_key == "incoming_supplier_invoice" and callable(
		getattr(adapter, "list_incoming_invoices", None)
	):
		return lambda configuration, provider: fetch_incoming_invoices_with_backend(
			configuration, provider, adapter
		)
	return getattr(adapter, POLLING_JOB_MAP[job_key].dispatch_method, None)


def poll_outbound_statuses_with_backend(configuration, provider, adapter) -> dict[str, int]:
	rows = frappe.get_all(
		"EDI Document",
		filters={
			"company": configuration.company,
			"provider": provider.name,
			"document_kind": ["in", ["invoice", "credit_note", "autofattura"]],
		},
		fields=["name", "external_submission_id", "transmission_state"],
	)
	processed = 0
	updated = 0
	terminal_states = set(getattr(adapter, "terminal_transmission_states", ()))

	for row in rows:
		if not row.get("external_submission_id"):
			continue
		if row.get("transmission_state") in terminal_states:
			continue

		invoice = adapter.get_outbound_invoice(provider, row["external_submission_id"])
		document = frappe.get_doc("EDI Document", row["name"])
		processed += 1
		updated += int(sync_outbound_invoice(document, adapter, invoice))

	return {"processed": processed, "updated": updated}


def fetch_notifications_with_backend(configuration, provider, adapter) -> dict[str, int]:
	rows = frappe.get_all(
		"EDI Document",
		filters={
			"company": configuration.company,
			"provider": provider.name,
			"document_kind": ["in", ["invoice", "credit_note", "autofattura"]],
		},
		fields=["name", "external_submission_id"],
	)
	processed = 0
	updated = 0

	for row in rows:
		if not row.get("external_submission_id"):
			continue

		invoice = adapter.get_outbound_invoice(provider, row["external_submission_id"])
		notifications = adapter.iter_invoice_notifications(provider, invoice)
		if not notifications:
			continue

		document = frappe.get_doc("EDI Document", row["name"])
		for notification in notifications:
			processed += 1
			updated += int(sync_notification(document, adapter, notification))

	return {"processed": processed, "updated": updated}


def fetch_incoming_invoices_with_backend(configuration, provider, adapter) -> dict[str, int]:
	invoices = adapter.list_incoming_invoices(configuration, provider)
	processed = 0
	created = 0
	updated = 0

	for invoice in invoices:
		result = upsert_incoming_invoice_document(configuration, provider, adapter, invoice)
		processed += 1
		created += int(result["created"])
		updated += int(result["updated"])

	return {"processed": processed, "created": created, "updated": updated}


def update_polling_timestamp(configuration_name: str, fieldname: str, retries: int = 3) -> None:
	last_error = None
	for attempt in range(retries):
		try:
			frappe.db.set_value(
				"EDI Configuration",
				configuration_name,
				fieldname,
				now_datetime(),
				update_modified=False,
			)
			return
		except frappe.QueryDeadlockError as exc:
			last_error = exc
			if attempt == retries - 1:
				break
			frappe.db.rollback()
			sleep(0.2 * (attempt + 1))

	if last_error:
		raise last_error
