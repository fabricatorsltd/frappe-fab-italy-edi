from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fab_italy_edi.polling import (
	fetch_notifications_with_backend,
	get_due_polling_jobs,
	poll_outbound_statuses_with_backend,
	run_polling_job,
)


class TestAutomaticPolling(unittest.TestCase):
	def test_all_jobs_are_due_when_never_run(self):
		now = datetime(2026, 4, 24, 17, 0, 0)
		configuration = {
			"auto_refresh_enabled": 1,
			"outbound_status_polling_interval_mins": 15,
			"incoming_invoice_polling_interval_mins": 60,
			"notification_polling_interval_mins": 15,
			"last_outbound_status_polling_at": None,
			"last_incoming_invoice_polling_at": None,
			"last_notification_polling_at": None,
		}

		due_jobs = {job.job_key for job in get_due_polling_jobs(configuration, now=now)}

		self.assertEqual(
			due_jobs,
			{"outbound_status", "incoming_supplier_invoice", "notification"},
		)

	def test_only_expired_jobs_are_due(self):
		now = datetime(2026, 4, 24, 17, 0, 0)
		configuration = {
			"auto_refresh_enabled": 1,
			"outbound_status_polling_interval_mins": 15,
			"incoming_invoice_polling_interval_mins": 60,
			"notification_polling_interval_mins": 15,
			"last_outbound_status_polling_at": now - timedelta(minutes=20),
			"last_incoming_invoice_polling_at": now - timedelta(minutes=30),
			"last_notification_polling_at": now - timedelta(minutes=15),
		}

		due_jobs = {job.job_key for job in get_due_polling_jobs(configuration, now=now)}

		self.assertEqual(due_jobs, {"outbound_status", "notification"})

	def test_run_polling_job_updates_timestamp_only_after_handler_runs(self):
		configuration = SimpleNamespace(
			name="fabricators",
			enabled=1,
			auto_refresh_enabled=1,
			default_provider="OpenAPI SDI Proxy",
		)
		provider = SimpleNamespace(enabled=1, use_status_polling=1, adapter_key="openapi")
		adapter = SimpleNamespace(poll_outbound_statuses=Mock(return_value={"ok": True}))
		db = SimpleNamespace(set_value=Mock())

		@contextmanager
		def fake_automation_user_context():
			yield "fab-edi-bot@example.invalid"

		with (
			patch("fab_italy_edi.polling.frappe.get_doc", side_effect=[configuration, provider]),
			patch("fab_italy_edi.polling.get_provider_adapter", return_value=adapter),
			patch("fab_italy_edi.polling.automation_user_context", fake_automation_user_context),
			patch("fab_italy_edi.polling.now_datetime", return_value=datetime(2026, 4, 24, 17, 0, 0)),
			patch("fab_italy_edi.polling.frappe.db", new=db),
		):
			result = run_polling_job("fabricators", "outbound_status")

		self.assertEqual(result, {"ok": True})
		adapter.poll_outbound_statuses.assert_called_once_with(
			configuration=configuration,
			provider=provider,
		)
		db.set_value.assert_called_once()

	def test_run_polling_job_uses_automation_user_context(self):
		configuration = SimpleNamespace(
			name="fabricators",
			enabled=1,
			auto_refresh_enabled=1,
			default_provider="OpenAPI SDI Proxy",
		)
		provider = SimpleNamespace(enabled=1, use_status_polling=1, adapter_key="openapi")
		adapter = SimpleNamespace(poll_outbound_statuses=Mock(return_value={"ok": True}))
		db = SimpleNamespace(set_value=Mock())
		markers = []

		@contextmanager
		def fake_automation_user_context():
			markers.append("enter")
			yield "fab-edi-bot@example.invalid"
			markers.append("exit")

		with (
			patch("fab_italy_edi.polling.frappe.get_doc", side_effect=[configuration, provider]),
			patch("fab_italy_edi.polling.get_provider_adapter", return_value=adapter),
			patch("fab_italy_edi.polling.automation_user_context", fake_automation_user_context),
			patch("fab_italy_edi.polling.now_datetime", return_value=datetime(2026, 4, 24, 17, 0, 0)),
			patch("fab_italy_edi.polling.frappe.db", new=db),
		):
			run_polling_job("fabricators", "outbound_status")

		self.assertEqual(markers, ["enter", "exit"])

	def test_run_polling_job_does_not_update_timestamp_without_handler(self):
		configuration = SimpleNamespace(
			name="fabricators",
			enabled=1,
			auto_refresh_enabled=1,
			default_provider="OpenAPI SDI Proxy",
		)
		provider = SimpleNamespace(enabled=1, use_status_polling=1, adapter_key="openapi")
		adapter = SimpleNamespace()
		db = SimpleNamespace(set_value=Mock())

		with (
			patch("fab_italy_edi.polling.frappe.get_doc", side_effect=[configuration, provider]),
			patch("fab_italy_edi.polling.get_provider_adapter", return_value=adapter),
			patch("fab_italy_edi.polling.frappe.db", new=db),
		):
			result = run_polling_job("fabricators", "outbound_status")

		self.assertIsNone(result)
		db.set_value.assert_not_called()

	def test_outbound_polling_includes_autofattura_documents(self):
		configuration = SimpleNamespace(company="fabricators")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy")
		adapter = SimpleNamespace(
			terminal_transmission_states={"accepted"},
			get_outbound_invoice=Mock(return_value={"uuid": "inv-123"}),
			normalize_outbound_invoice=Mock(return_value={"receipt_state": "queued", "transmission_state": "queued"}),
		)
		document = SimpleNamespace()

		with (
			patch("fab_italy_edi.polling.frappe.get_all", return_value=[{"name": "EDI-1", "external_submission_id": "inv-123", "transmission_state": "queued"}]) as get_all,
			patch("fab_italy_edi.polling.frappe.get_doc", return_value=document),
			patch("fab_italy_edi.polling.sync_outbound_invoice", return_value=False) as sync_outbound_invoice,
		):
			result = poll_outbound_statuses_with_backend(configuration, provider, adapter)

		self.assertEqual(result, {"processed": 1, "updated": 0})
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["document_kind"],
			["in", ["invoice", "credit_note", "autofattura"]],
		)
		sync_outbound_invoice.assert_called_once_with(document, adapter, {"uuid": "inv-123"})

	def test_notification_polling_excludes_supplier_import_documents(self):
		configuration = SimpleNamespace(company="fabricators")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy")
		adapter = SimpleNamespace(
			get_outbound_invoice=Mock(return_value={"uuid": "inv-123"}),
			iter_invoice_notifications=Mock(return_value=[]),
		)

		with patch("fab_italy_edi.polling.frappe.get_all", return_value=[] ) as get_all:
			result = fetch_notifications_with_backend(configuration, provider, adapter)

		self.assertEqual(result, {"processed": 0, "updated": 0})
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["document_kind"],
			["in", ["invoice", "credit_note", "autofattura"]],
		)
