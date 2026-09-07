from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fab_italy_edi.polling_watchdog import (
	check_polling_health,
	get_polling_alert_recipients,
	get_stale_threshold_minutes,
	is_real_email_address,
	review_polling_health,
)

NOW = datetime(2026, 9, 7, 9, 0, 0)


def make_configuration(**overrides):
	values = {
		"name": "fabricators",
		"company": "FABRICATORS S.R.L.",
		"enabled": 1,
		"default_provider": "OpenAPI SDI Proxy",
		"auto_refresh_enabled": 1,
		"outbound_status_polling_interval_mins": 120,
		"incoming_invoice_polling_interval_mins": 120,
		"notification_polling_interval_mins": 120,
		"last_outbound_status_polling_at": NOW - timedelta(minutes=10),
		"last_incoming_invoice_polling_at": NOW - timedelta(minutes=10),
		"last_notification_polling_at": NOW - timedelta(minutes=10),
		"polling_alert_recipients": "ops@fabricators.ltd",
		"last_polling_alert_sent_at": None,
		"polling_alert_active": 0,
		"modified": NOW - timedelta(days=30),
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def make_adapter():
	return SimpleNamespace(
		get_outbound_invoice=Mock(),
		iter_invoice_notifications=Mock(),
		list_incoming_invoices=Mock(),
	)


class WatchdogHarness:
	def __init__(self, configuration, provider=None, error_logs=None, adapter=None):
		self.configuration = configuration
		self.provider = provider if provider is not None else SimpleNamespace(enabled=1, use_status_polling=1)
		self.error_logs = error_logs or []
		self.adapter = adapter if adapter is not None else make_adapter()
		self.sendmail = Mock()
		self.db = SimpleNamespace(
			set_value=Mock(),
			get_value=Mock(return_value=self.provider),
			commit=Mock(),
			rollback=Mock(),
		)

	def __enter__(self):
		self.patches = [
			patch("fab_italy_edi.polling_watchdog.frappe.get_doc", return_value=self.configuration),
			patch("fab_italy_edi.polling_watchdog.frappe.get_all", return_value=self.error_logs),
			patch("fab_italy_edi.polling_watchdog.get_provider_adapter", return_value=self.adapter),
			patch("fab_italy_edi.polling_watchdog.frappe.sendmail", new=self.sendmail),
			patch("fab_italy_edi.polling_watchdog.frappe.db", new=self.db),
		]
		for entry in self.patches:
			entry.start()
		return self

	def __exit__(self, *args):
		for entry in reversed(self.patches):
			entry.stop()
		return False


class TestPollingWatchdog(unittest.TestCase):
	def test_threshold_never_fires_early_and_never_waits_two_days(self):
		# The doctype validates the interval against a 5 minute minimum and a one day maximum.
		for interval in (5, 15, 60, 120, 240, 480, 720, 1440):
			with self.subTest(interval=interval):
				threshold = get_stale_threshold_minutes(interval)
				self.assertGreater(threshold, interval)
				self.assertLess(threshold, 2 * 24 * 60)
				if interval <= 720:
					self.assertGreaterEqual(threshold, 2 * interval)

	def test_one_missed_cycle_does_not_alert(self):
		# Due at 02:00 past the last run and missed, so the job is late but not yet stale.
		configuration = make_configuration(last_incoming_invoice_polling_at=NOW - timedelta(hours=3))

		with WatchdogHarness(configuration) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_job_without_a_configured_interval_is_not_watched(self):
		configuration = make_configuration(
			incoming_invoice_polling_interval_mins=0,
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
		)

		with WatchdogHarness(configuration) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_configuration_without_a_default_provider_sends_nothing(self):
		configuration = make_configuration(
			default_provider=None,
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
		)

		with WatchdogHarness(configuration) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_missing_provider_row_sends_nothing(self):
		configuration = make_configuration(last_incoming_invoice_polling_at=NOW - timedelta(days=5))

		with WatchdogHarness(configuration) as harness:
			harness.db.get_value.return_value = None
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_check_polling_health_never_raises_into_the_scheduler(self):
		db = SimpleNamespace(commit=Mock(), rollback=Mock())

		with (
			patch("fab_italy_edi.polling_watchdog.frappe.get_all", return_value=["fabricators"]),
			patch(
				"fab_italy_edi.polling_watchdog.review_polling_health",
				side_effect=RuntimeError("provider exploded"),
			),
			patch("fab_italy_edi.polling_watchdog.frappe.db", new=db),
			patch("fab_italy_edi.polling_watchdog.log_watchdog_failure") as log_failure,
		):
			check_polling_health()

		log_failure.assert_called_once_with("fabricators")
		db.rollback.assert_called_once()

	def test_adapter_without_a_handler_sends_nothing(self):
		configuration = make_configuration(
			last_outbound_status_polling_at=NOW - timedelta(days=5),
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
			last_notification_polling_at=NOW - timedelta(days=5),
		)

		with WatchdogHarness(configuration, adapter=SimpleNamespace()) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_disabled_provider_sends_nothing(self):
		configuration = make_configuration(last_incoming_invoice_polling_at=NOW - timedelta(days=5))
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=0, use_status_polling=1)

		with WatchdogHarness(configuration, provider=provider) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_switching_polling_off_clears_the_flag_without_a_recovery_mail(self):
		configuration = make_configuration(
			auto_refresh_enabled=0,
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
			last_polling_alert_sent_at=NOW - timedelta(hours=2),
			polling_alert_active=1,
		)

		with WatchdogHarness(configuration) as harness:
			review_polling_health("fabricators", now=NOW)

		harness.sendmail.assert_not_called()
		harness.db.set_value.assert_called_once_with(
			"EDI Configuration",
			"fabricators",
			{"polling_alert_active": 0},
			update_modified=False,
		)

	def test_a_failed_send_still_consumes_the_rate_limit(self):
		configuration = make_configuration(last_incoming_invoice_polling_at=NOW - timedelta(hours=8))

		with WatchdogHarness(configuration) as harness:
			harness.sendmail.side_effect = RuntimeError("no default outgoing email account")
			with patch("fab_italy_edi.polling_watchdog.log_watchdog_failure") as log_failure:
				review_polling_health("fabricators", now=NOW)

		log_failure.assert_called_once_with("fabricators")
		harness.db.set_value.assert_called_once_with(
			"EDI Configuration",
			"fabricators",
			{"last_polling_alert_sent_at": NOW, "polling_alert_active": 0},
			update_modified=False,
		)

	def test_stale_job_sends_one_alert_naming_the_job(self):
		configuration = make_configuration(last_incoming_invoice_polling_at=NOW - timedelta(hours=8))

		with WatchdogHarness(
			configuration,
			error_logs=[{"method": "Unable to fetch supplier invoices", "creation": NOW - timedelta(hours=3)}],
		) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual([entry["job"].job_key for entry in stale_jobs], ["incoming_supplier_invoice"])
		harness.sendmail.assert_called_once()
		message = harness.sendmail.call_args.kwargs["message"]
		self.assertEqual(harness.sendmail.call_args.kwargs["recipients"], ["ops@fabricators.ltd"])
		self.assertIn("FABRICATORS S.R.L.", harness.sendmail.call_args.kwargs["subject"])
		self.assertIn("Incoming supplier invoice polling", message)
		self.assertIn("6h behind schedule", message)
		self.assertIn("Unable to fetch supplier invoices", message)
		harness.db.set_value.assert_called_once_with(
			"EDI Configuration",
			"fabricators",
			{"last_polling_alert_sent_at": NOW, "polling_alert_active": 1},
			update_modified=False,
		)

	def test_fresh_jobs_send_nothing(self):
		with WatchdogHarness(make_configuration()) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()
		harness.db.set_value.assert_not_called()

	def test_deliberately_disabled_job_sends_nothing(self):
		configuration = make_configuration(
			auto_refresh_enabled=0,
			last_outbound_status_polling_at=NOW - timedelta(days=5),
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
			last_notification_polling_at=NOW - timedelta(days=5),
		)

		with WatchdogHarness(configuration) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual(stale_jobs, [])
		harness.sendmail.assert_not_called()

	def test_provider_without_status_polling_only_reports_the_incoming_job(self):
		configuration = make_configuration(
			last_outbound_status_polling_at=NOW - timedelta(days=5),
			last_incoming_invoice_polling_at=NOW - timedelta(days=5),
			last_notification_polling_at=NOW - timedelta(days=5),
		)
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=1, use_status_polling=0)

		with WatchdogHarness(configuration, provider=provider):
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual([entry["job"].job_key for entry in stale_jobs], ["incoming_supplier_invoice"])

	def test_second_run_within_24_hours_sends_nothing(self):
		configuration = make_configuration(
			last_incoming_invoice_polling_at=NOW - timedelta(hours=8),
			last_polling_alert_sent_at=NOW - timedelta(hours=1),
			polling_alert_active=1,
		)

		with WatchdogHarness(configuration) as harness:
			stale_jobs = review_polling_health("fabricators", now=NOW)

		self.assertEqual([entry["job"].job_key for entry in stale_jobs], ["incoming_supplier_invoice"])
		harness.sendmail.assert_not_called()
		harness.db.set_value.assert_not_called()

	def test_recovered_job_sends_one_recovery_alert(self):
		configuration = make_configuration(
			last_polling_alert_sent_at=NOW - timedelta(hours=1),
			polling_alert_active=1,
		)

		with WatchdogHarness(configuration) as harness:
			review_polling_health("fabricators", now=NOW)

		harness.sendmail.assert_called_once()
		self.assertIn("recovered", harness.sendmail.call_args.kwargs["subject"])
		harness.db.set_value.assert_called_once_with(
			"EDI Configuration",
			"fabricators",
			{"polling_alert_active": 0},
			update_modified=False,
		)

	def test_recipients_fall_back_to_enabled_system_managers(self):
		configuration = make_configuration(polling_alert_recipients=None)
		rows = [
			["Administrator", "pietro@fabricators.ltd", "ops@fabricators.ltd"],
			[
				{"name": "Administrator", "email": "admin@example.com"},
				{"name": "pietro@fabricators.ltd", "email": "pietro@fabricators.ltd"},
				{"name": "ops@fabricators.ltd", "email": "ops@fabricators.ltd"},
			],
		]

		with patch("fab_italy_edi.polling_watchdog.frappe.get_all", side_effect=rows) as get_all:
			recipients = get_polling_alert_recipients(configuration)

		self.assertEqual(recipients, ["pietro@fabricators.ltd", "ops@fabricators.ltd"])
		# Disabled users cannot be filtered out in a mocked query, so guard the filter instead.
		self.assertEqual(
			get_all.call_args_list[1].kwargs["filters"],
			{"name": ["in", rows[0]], "enabled": 1, "user_type": "System User"},
		)

	def test_placeholder_addresses_are_never_used(self):
		for address in ("admin@example.com", "fab-edi-bot@example.invalid", "Administrator", "", None):
			with self.subTest(address=address):
				self.assertFalse(is_real_email_address(address))

		self.assertTrue(is_real_email_address("pietro@fabricators.ltd"))
