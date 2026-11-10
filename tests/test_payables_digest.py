import unittest
from datetime import date
from unittest.mock import patch

import frappe

from fab_italy_edi import payables_digest

TODAY = date(2026, 11, 10)


def row(number, deadline, doctype="Purchase Invoice"):
	return frappe._dict(
		doctype=doctype,
		name=number,
		number=number,
		party="Hetzner Online GmbH",
		detail="Invoice 123",
		amount="EUR 24.38",
		deadline=deadline,
	)


def section_numbers(sections):
	return {key: [item.number for item in rows] for key, rows in sections}


class TestBuckets(unittest.TestCase):
	def test_supplier_invoices_by_days_left(self):
		sections = payables_digest.bucket_invoices(
			[
				row("D-32", date(2026, 12, 12)),
				row("D-31", date(2026, 12, 11)),
				row("D-16", date(2026, 11, 26)),
				row("D-15", date(2026, 11, 25)),
				row("D-8", date(2026, 11, 18)),
				row("D-7", date(2026, 11, 17)),
				row("D-0", TODAY),
				row("LATE-1", date(2026, 11, 9)),
				row("LATE-30", date(2026, 10, 11)),
			],
			TODAY,
		)
		self.assertEqual(
			section_numbers(sections),
			{
				payables_digest.OVERDUE: ["LATE-30", "LATE-1"],
				payables_digest.WITHIN_7: ["D-0", "D-7"],
				payables_digest.WITHIN_15: ["D-8", "D-15"],
				payables_digest.WITHIN_31: ["D-16", "D-31"],
			},
		)
		self.assertEqual(
			[key for key, _rows in sections],
			[payables_digest.OVERDUE, payables_digest.WITHIN_7, payables_digest.WITHIN_15, payables_digest.WITHIN_31],
		)

	def test_f24_overdue_and_this_month_only(self):
		sections = payables_digest.bucket_f24(
			[
				row("F24-NEXT", date(2026, 12, 16), "F24 Payment"),
				row("F24-NOV", date(2026, 11, 16), "F24 Payment"),
				row("F24-LAST-DAY", date(2026, 11, 30), "F24 Payment"),
				row("F24-OCT", date(2026, 10, 16), "F24 Payment"),
			],
			TODAY,
		)
		self.assertEqual(
			section_numbers(sections),
			{
				payables_digest.F24_OVERDUE: ["F24-OCT"],
				payables_digest.F24_THIS_MONTH: ["F24-NOV", "F24-LAST-DAY"],
			},
		)

	def test_empty_sections_are_left_out(self):
		self.assertEqual(payables_digest.bucket_invoices([row("D-40", date(2026, 12, 20))], TODAY), [])
		self.assertEqual(payables_digest.bucket_f24([], TODAY), [])


class TestF24Rows(unittest.TestCase):
	def test_a_calendar_deadline_covered_by_a_draft_f24_is_listed_once(self):
		drafts = [
			frappe._dict(name="F24-2026-00037", data_versamento=date(2026, 11, 16), saldo_finale=1770.0, tax_calendar_event="TAX-EVENT-2026-00003")
		]
		events = [
			frappe._dict(name="TAX-EVENT-2026-00003", event_type="VAT Payment", event_date=date(2026, 11, 16), amount=900.0, notes=""),
			frappe._dict(name="TAX-EVENT-2026-00004", event_type="F24 Deadline", event_date=date(2026, 11, 30), amount=120.0, notes="IMU"),
		]

		def get_all(doctype, **kwargs):
			return drafts if doctype == "F24 Payment" else events

		with (
			patch.object(payables_digest.frappe.db, "exists", return_value=True),
			patch.object(payables_digest.frappe, "get_all", side_effect=get_all),
		):
			rows = payables_digest.get_f24_rows("FABRICATORS S.R.L.")
		self.assertEqual([r.name for r in rows], ["F24-2026-00037", "TAX-EVENT-2026-00004"])
		self.assertEqual(rows[1].party, "IMU")


class TestDigestMail(unittest.TestCase):
	def test_nothing_due_means_no_mail(self):
		with (
			patch.object(payables_digest, "get_invoice_rows", return_value=[row("D-40", date(2026, 12, 20))]),
			patch.object(payables_digest, "get_f24_rows", return_value=[]),
			patch.object(payables_digest.frappe, "sendmail") as sendmail,
		):
			self.assertFalse(payables_digest.send_digest("FABRICATORS S.R.L.", TODAY))
		sendmail.assert_not_called()

	def test_rows_are_mailed_to_the_alert_recipients(self):
		with (
			patch.object(payables_digest, "get_invoice_rows", return_value=[row("ACQ/2026/00251", date(2026, 10, 17))]),
			patch.object(payables_digest, "get_f24_rows", return_value=[]),
			patch.object(payables_digest, "get_rejection_alert_recipients", return_value=["billing@fabricators.ltd"]),
			patch.object(payables_digest.frappe, "sendmail") as sendmail,
		):
			self.assertTrue(payables_digest.send_digest("FABRICATORS S.R.L.", TODAY))
		sendmail.assert_called_once()
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["billing@fabricators.ltd"])

	def test_the_message_lists_sections_with_counts_and_dates(self):
		sections = [
			*payables_digest.bucket_invoices([row("ACQ/2026/00251", date(2026, 10, 17))], TODAY),
			*payables_digest.bucket_f24([row("F24-2026-00037", date(2026, 11, 16), "F24 Payment")], TODAY),
		]
		with patch.object(payables_digest, "get_url_to_form", return_value="https://erp/app/x/X"):
			subject, message = payables_digest.build_digest(sections, TODAY)
		self.assertTrue(subject.startswith("[Payments due] "))
		self.assertLess(message.index("Overdue supplier invoices (1)"), message.index("F24 due this month (1)"))
		self.assertNotIn("Overdue F24", message)
		self.assertIn("overdue since 17/10", message)
		self.assertIn("due 16/11", message)
		self.assertIn('href="https://erp/app/x/X"', message)


if __name__ == "__main__":
	unittest.main()
