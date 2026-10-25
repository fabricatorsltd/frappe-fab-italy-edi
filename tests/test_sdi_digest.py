import unittest
from datetime import date
from unittest.mock import patch

import frappe

from fab_italy_edi import sdi_digest

# a Friday, so the next two working days run through Tuesday the 27th
FRIDAY = date(2026, 10, 23)


def row(number, deadline=None, status=sdi_digest.LATER, reason="", failed=False):
	return frappe._dict(
		doctype="Sales Invoice",
		name=number,
		number=number,
		party="Royal Yacht International",
		kind=sdi_digest.INVOICE,
		amount="EUR 1,000.00",
		deadline=deadline,
		status=status,
		failed=failed,
		reason=reason,
	)


def section_numbers(sections):
	return {key: [item.number for item in rows] for key, rows in sections}


class TestDeadlines(unittest.TestCase):
	def test_a_sales_invoice_is_due_twelve_days_after_its_date(self):
		self.assertEqual(sdi_digest.sales_invoice_deadline("2026-10-20"), date(2026, 11, 1))

	def test_an_autofattura_is_due_on_the_fifteenth_of_the_next_month(self):
		self.assertEqual(sdi_digest.autofattura_deadline("2026-10-31"), date(2026, 11, 15))
		self.assertEqual(sdi_digest.autofattura_deadline("2026-12-01"), date(2027, 1, 15))

	def test_soon_skips_the_weekend(self):
		self.assertEqual(sdi_digest.soon_horizon(FRIDAY), date(2026, 10, 27))
		self.assertEqual(sdi_digest.soon_horizon(date(2026, 10, 20)), date(2026, 10, 22))


class TestStatus(unittest.TestCase):
	def test_unsent_states(self):
		for state in (None, "", "draft", "Ready"):
			self.assertEqual(sdi_digest.get_status(state, None), sdi_digest.LATER, state)

	def test_handed_to_sdi_counts_as_sent(self):
		for state in ("queued", "sending", "sent", "delivered", "accepted"):
			self.assertIsNone(sdi_digest.get_status(state, None), state)
		self.assertIsNone(sdi_digest.get_status("ready", "delivered"))
		self.assertIsNone(sdi_digest.get_status("cancelled", None))

	def test_rejected_and_failed_on_either_state(self):
		self.assertEqual(sdi_digest.get_status("rejected", None), sdi_digest.REJECTED)
		self.assertEqual(sdi_digest.get_status("sent", "rejected"), sdi_digest.REJECTED)
		self.assertEqual(sdi_digest.get_status("failed", None), sdi_digest.REJECTED)
		self.assertTrue(sdi_digest.is_failed("failed", None))
		self.assertFalse(sdi_digest.is_failed("sent", "rejected"))


class TestBuckets(unittest.TestCase):
	def test_each_row_lands_in_one_section_in_mail_order(self):
		rows = [
			row("LATER", date(2026, 10, 28)),
			row("SOON-TUE", date(2026, 10, 27)),
			row("TODAY", FRIDAY),
			row("LATE", date(2026, 10, 22)),
			row("REJECTED", date(2026, 10, 1), status=sdi_digest.REJECTED, reason="00200 bad CAP"),
			row("NO-DATE"),
		]
		sections = sdi_digest.bucket_rows(rows, FRIDAY)
		self.assertEqual(
			section_numbers(sections),
			{
				sdi_digest.REJECTED: ["REJECTED"],
				sdi_digest.OVERDUE: ["LATE"],
				sdi_digest.SOON: ["TODAY", "SOON-TUE"],
				sdi_digest.LATER: ["LATER", "NO-DATE"],
			},
		)
		self.assertEqual([key for key, _rows in sections], list(sdi_digest.SECTION_ORDER))

	def test_empty_sections_are_left_out(self):
		sections = sdi_digest.bucket_rows([row("LATE", date(2026, 10, 1))], FRIDAY)
		self.assertEqual([key for key, _rows in sections], [sdi_digest.OVERDUE])
		self.assertEqual(sdi_digest.bucket_rows([], FRIDAY), [])


class TestDigestMail(unittest.TestCase):
	def test_no_rows_means_no_mail(self):
		with (
			patch.object(sdi_digest, "collect_rows", return_value=[]),
			patch.object(sdi_digest.frappe, "sendmail") as sendmail,
		):
			self.assertFalse(sdi_digest.send_digest("FABRICATORS S.R.L.", FRIDAY))
		sendmail.assert_not_called()

	def test_rows_are_mailed_to_the_alert_recipients(self):
		with (
			patch.object(sdi_digest, "collect_rows", return_value=[row("FATT/2026/00034", date(2026, 10, 1))]),
			patch.object(sdi_digest, "get_rejection_alert_recipients", return_value=["billing@fabricators.ltd"]),
			patch.object(sdi_digest.frappe, "sendmail") as sendmail,
		):
			self.assertTrue(sdi_digest.send_digest("FABRICATORS S.R.L.", FRIDAY))
		sendmail.assert_called_once()
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["billing@fabricators.ltd"])

	def test_the_message_lists_sections_with_counts_and_the_reason(self):
		sections = sdi_digest.bucket_rows(
			[
				row("FATT/2026/00040", date(2026, 10, 20)),
				row("FATT/2026/00041", date(2026, 10, 1), status=sdi_digest.REJECTED, reason="00200 bad CAP"),
			],
			FRIDAY,
		)
		with patch.object(sdi_digest, "get_url_to_form", return_value="https://erp/app/sales-invoice/X"):
			subject, message = sdi_digest.build_digest(sections, FRIDAY)
		self.assertTrue(subject.startswith("[Invoices to send to SdI] "))
		self.assertLess(message.index("Rejected by SdI (1)"), message.index("Send overdue (1)"))
		self.assertNotIn("To send within 2 days", message)
		self.assertIn("00200 bad CAP", message)
		self.assertIn("overdue by 3 days", message)
		self.assertIn('href="https://erp/app/sales-invoice/X"', message)

	def test_deadline_labels(self):
		self.assertEqual(sdi_digest.deadline_label(row("A", date(2026, 10, 22)), FRIDAY), "overdue by 1 day")
		self.assertEqual(sdi_digest.deadline_label(row("A", date(2026, 10, 27)), FRIDAY), "due 27/10")
		self.assertEqual(sdi_digest.deadline_label(row("A", status=sdi_digest.REJECTED, failed=True), FRIDAY), "Send failed")

	def test_kind_labels(self):
		self.assertEqual(sdi_digest.kind_label(frappe._dict(kind=sdi_digest.CREDIT_NOTE)), "Credit note")
		self.assertEqual(sdi_digest.kind_label(frappe._dict(kind=sdi_digest.AUTOFATTURA, document_type="TD17")), "Autofattura TD17")
		self.assertEqual(sdi_digest.kind_label(frappe._dict(kind=sdi_digest.AUTOFATTURA_TO_PREPARE)), "Autofattura to prepare")


def sales_invoice(name, posting_date, state=None, receipt_state=None, edi_document=None, is_return=0):
	return frappe._dict(
		name=name,
		customer="CUST",
		customer_name="Customer",
		posting_date=posting_date,
		is_return=is_return,
		grand_total=100,
		currency="EUR",
		fab_edi_transmission_state=state,
		fab_edi_receipt_state=receipt_state,
		fab_edi_document=edi_document,
	)


class TestSalesInvoiceRows(unittest.TestCase):
	def rows(self, invoices):
		with (
			patch.object(sdi_digest.frappe, "get_all", return_value=invoices),
			patch.object(sdi_digest, "get_reason", return_value="reason"),
		):
			return {item.number: item for item in sdi_digest.get_sales_invoice_rows("FABRICATORS S.R.L.", date(2026, 7, 24))}

	def test_the_cutoff_hides_older_unsent_invoices_but_not_rejections(self):
		rows = self.rows(
			[
				sales_invoice("OLD", date(2026, 7, 1)),
				sales_invoice("NEW", date(2026, 7, 26), is_return=1),
				sales_invoice("SENT", date(2026, 8, 1), state="sent"),
				sales_invoice("REJECTED-OLD", date(2026, 7, 1), state="rejected", edi_document="EDI-1"),
			]
		)
		self.assertEqual(set(rows), {"NEW", "REJECTED-OLD"})
		self.assertEqual(rows["NEW"].kind, sdi_digest.CREDIT_NOTE)
		self.assertEqual(rows["NEW"].deadline, date(2026, 8, 7))
		self.assertEqual(rows["REJECTED-OLD"].status, sdi_digest.REJECTED)

	def test_rejections_imported_from_odoo_are_closed_history(self):
		rows = self.rows([sales_invoice("ODOO", date(2026, 8, 1), state="sent", receipt_state="failed")])
		self.assertEqual(rows, {})
