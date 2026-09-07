from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import getdate

from fab_italy_edi.sales_invoice_edi import keep_manual_due_date, set_due_date_from_payment_schedule


POSTING_DATE = "2026-09-07"
TEMPLATE_DUE_DATE = "2026-10-07"
TEMPLATE = "Bonifico 30 gg d.f."


class FakeDocument(frappe._dict):
	def get_doc_before_save(self):
		return self.stored

	def set(self, fieldname, value):
		self[fieldname] = value


def make_row(name: str, due_date: str, payment_term: str | None = None, credit_days: int = 0):
	return frappe._dict(
		name=name,
		due_date=due_date,
		payment_term=payment_term,
		due_date_based_on="Day(s) after invoice date" if payment_term else None,
		credit_days=credit_days,
		credit_months=0,
		mode_of_payment="Wire Transfer",
		invoice_portion=100,
	)


def make_invoice(due_date, rows, template=None, stored=None, **fields):
	invoice = FakeDocument(
		doctype="Sales Invoice",
		posting_date=POSTING_DATE,
		due_date=due_date,
		payment_terms_template=template,
		payment_schedule=rows,
		stored=stored,
	)
	invoice.update(fields)
	return invoice


def template_terms(*due_dates: str):
	return [frappe._dict(due_date=due_date) for due_date in due_dates]


def stored_state(due_date: str, *rows: tuple[str, str], template: str | None = None):
	return frappe._dict(
		due_date=due_date,
		payment_terms_template=template,
		payment_schedule=[frappe._dict(name=name, due_date=row_due_date) for name, row_due_date in rows],
	)


class TestSalesInvoiceDueDate(unittest.TestCase):
	def test_schedule_that_still_follows_the_template_keeps_it(self):
		row = make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(TEMPLATE_DUE_DATE, [row], template=TEMPLATE)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms(TEMPLATE_DUE_DATE),
		):
			keep_manual_due_date(invoice)

		self.assertEqual(invoice.payment_terms_template, TEMPLATE)
		self.assertEqual(row.due_date, TEMPLATE_DUE_DATE)
		self.assertEqual(row.payment_term, "Bonifico 30 gg d.f. 100%")

	def test_template_sets_the_due_date_from_the_schedule(self):
		invoice = make_invoice(
			POSTING_DATE, [make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%")]
		)

		set_due_date_from_payment_schedule(invoice)

		self.assertEqual(str(invoice.due_date), TEMPLATE_DUE_DATE)

	def test_due_date_typed_on_the_row_drops_the_template(self):
		"""The form syncs the single row, so only the template claim is left to clear."""
		row = make_row("row1", POSTING_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			POSTING_DATE,
			[row],
			template=TEMPLATE,
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms(TEMPLATE_DUE_DATE),
		):
			keep_manual_due_date(invoice)

		self.assertIsNone(invoice.payment_terms_template)
		self.assertEqual(row.due_date, POSTING_DATE)
		self.assertIsNone(row.payment_term)
		self.assertIsNone(row.due_date_based_on)
		self.assertEqual(row.credit_days, 0)
		self.assertEqual(row.mode_of_payment, "Wire Transfer")

	def test_due_date_typed_alone_moves_the_schedule_row(self):
		"""A save through the api that only moves the due date must not be undone by the schedule."""
		row = make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			POSTING_DATE,
			[row],
			template=TEMPLATE,
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms(TEMPLATE_DUE_DATE),
		):
			keep_manual_due_date(invoice)
		set_due_date_from_payment_schedule(invoice)

		self.assertIsNone(invoice.payment_terms_template)
		self.assertEqual(getdate(row.due_date), getdate(POSTING_DATE))
		self.assertEqual(str(invoice.due_date), POSTING_DATE)

	def test_manual_due_date_survives_the_next_save(self):
		"""The save after the override sends back what was stored, and nothing may move again."""
		row = make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			POSTING_DATE,
			[row],
			template=TEMPLATE,
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms(TEMPLATE_DUE_DATE),
		):
			keep_manual_due_date(invoice)
			set_due_date_from_payment_schedule(invoice)

			resaved = make_invoice(
				invoice.due_date,
				invoice.payment_schedule,
				template=invoice.payment_terms_template,
				stored=stored_state(invoice.due_date, ("row1", row.due_date)),
			)
			keep_manual_due_date(resaved)
			set_due_date_from_payment_schedule(resaved)

		self.assertIsNone(resaved.payment_terms_template)
		self.assertEqual(getdate(row.due_date), getdate(POSTING_DATE))
		self.assertEqual(str(resaved.due_date), POSTING_DATE)

	def test_schedule_refetched_by_the_client_leads_the_due_date(self):
		"""Moving the posting date rewrites the rows from the template, and that is not a manual date."""
		new_due_date = "2026-10-10"
		row = make_row("row1", new_due_date, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			new_due_date,
			[row],
			template=TEMPLATE,
			posting_date="2026-09-10",
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms", return_value=template_terms(new_due_date)
		):
			keep_manual_due_date(invoice)
		set_due_date_from_payment_schedule(invoice)

		self.assertEqual(invoice.payment_terms_template, TEMPLATE)
		self.assertEqual(getdate(row.due_date), getdate(new_due_date))
		self.assertEqual(str(invoice.due_date), new_due_date)

	def test_new_template_hands_the_schedule_back_to_erpnext(self):
		"""Picking a template through the api must rebuild the schedule, not lose the template."""
		row = make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			TEMPLATE_DUE_DATE,
			[row],
			template="Bonifico 60 gg d.f.",
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		keep_manual_due_date(invoice)

		self.assertEqual(invoice.payment_terms_template, "Bonifico 60 gg d.f.")
		self.assertEqual(invoice.payment_schedule, [])
		self.assertIsNone(invoice.due_date)

	def test_manual_due_date_moves_the_last_row_of_a_multi_row_schedule(self):
		first = make_row("row1", getdate("2026-10-30"), payment_term="Bonifico 30 gg 50%", credit_days=30)
		last = make_row("row2", getdate("2026-11-29"), payment_term="Bonifico 60 gg 50%", credit_days=60)
		invoice = make_invoice(
			"2026-12-31",
			[first, last],
			template="Bonifico 30/60 gg d.f.f.m.",
			stored=stored_state(
				"2026-11-29",
				("row1", "2026-10-30"),
				("row2", "2026-11-29"),
				template="Bonifico 30/60 gg d.f.f.m.",
			),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms("2026-10-30", "2026-11-29"),
		):
			keep_manual_due_date(invoice)
		set_due_date_from_payment_schedule(invoice)

		self.assertIsNone(invoice.payment_terms_template)
		self.assertEqual(len(invoice.payment_schedule), 2)
		self.assertEqual(getdate(first.due_date), getdate("2026-10-30"))
		self.assertEqual(first.payment_term, "Bonifico 30 gg 50%")
		self.assertEqual(getdate(last.due_date), getdate("2026-12-31"))
		self.assertIsNone(last.payment_term)
		self.assertEqual(str(invoice.due_date), "2026-12-31")

	def test_manual_due_date_before_an_instalment_collapses_the_schedule(self):
		"""One typed date cannot carry instalments that fall after it, so it replaces them."""
		first = make_row("row1", "2026-10-30", payment_term="Bonifico 30 gg 50%", credit_days=30)
		last = make_row("row2", "2026-11-29", payment_term="Bonifico 60 gg 50%", credit_days=60)
		invoice = make_invoice(
			"2026-09-27",
			[first, last],
			template="Bonifico 30/60 gg d.f.f.m.",
			stored=stored_state(
				"2026-11-29",
				("row1", "2026-10-30"),
				("row2", "2026-11-29"),
				template="Bonifico 30/60 gg d.f.f.m.",
			),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms("2026-10-30", "2026-11-29"),
		):
			keep_manual_due_date(invoice)
		set_due_date_from_payment_schedule(invoice)

		self.assertIsNone(invoice.payment_terms_template)
		self.assertEqual(invoice.payment_schedule, [last])
		self.assertEqual(getdate(last.due_date), getdate("2026-09-27"))
		self.assertEqual(last.invoice_portion, 100)
		self.assertEqual(last.mode_of_payment, "Wire Transfer")
		self.assertIsNone(last.payment_term)
		self.assertEqual(str(invoice.due_date), "2026-09-27")

	def test_reordered_rows_keep_the_template(self):
		first = make_row("row1", "2026-11-29", payment_term="Bonifico 60 gg 50%", credit_days=60)
		last = make_row("row2", "2026-10-30", payment_term="Bonifico 30 gg 50%", credit_days=30)
		invoice = make_invoice(
			"2026-11-29",
			[first, last],
			template="Bonifico 30/60 gg d.f.f.m.",
			stored=stored_state(
				"2026-11-29",
				("row1", "2026-11-29"),
				("row2", "2026-10-30"),
				template="Bonifico 30/60 gg d.f.f.m.",
			),
		)

		with patch(
			"fab_italy_edi.sales_invoice_edi.get_payment_terms",
			return_value=template_terms("2026-10-30", "2026-11-29"),
		):
			keep_manual_due_date(invoice)

		self.assertEqual(invoice.payment_terms_template, "Bonifico 30/60 gg d.f.f.m.")
		self.assertEqual(first.payment_term, "Bonifico 60 gg 50%")

	def test_invoice_that_ignores_the_default_template_is_left_alone(self):
		"""Terms taken from an order or an import are not ours to manage."""
		row = make_row("row1", TEMPLATE_DUE_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			POSTING_DATE,
			[row],
			template=TEMPLATE,
			ignore_default_payment_terms_template=1,
			stored=stored_state(TEMPLATE_DUE_DATE, ("row1", TEMPLATE_DUE_DATE), template=TEMPLATE),
		)

		keep_manual_due_date(invoice)

		self.assertEqual(invoice.payment_terms_template, TEMPLATE)
		self.assertEqual(row.due_date, TEMPLATE_DUE_DATE)
		self.assertEqual(row.payment_term, "Bonifico 30 gg d.f. 100%")

	def test_credit_note_is_left_alone(self):
		"""ERPNext empties template and schedule on a return, so there is nothing to override."""
		row = make_row("row1", POSTING_DATE, payment_term="Bonifico 30 gg d.f. 100%", credit_days=30)
		invoice = make_invoice(
			TEMPLATE_DUE_DATE,
			[row],
			template=TEMPLATE,
			is_return=1,
			stored=stored_state(POSTING_DATE, ("row1", POSTING_DATE), template=TEMPLATE),
		)

		keep_manual_due_date(invoice)

		self.assertEqual(invoice.payment_terms_template, TEMPLATE)
		self.assertEqual(invoice.due_date, TEMPLATE_DUE_DATE)
		self.assertEqual(row.payment_term, "Bonifico 30 gg d.f. 100%")

	def test_document_without_a_template_is_left_alone(self):
		"""No template and a hand set date: nothing to fill in, nothing to unlink."""
		row = make_row("row1", "2026-09-17", payment_term="Rimessa diretta 100%", credit_days=15)
		invoice = make_invoice(
			"2026-09-17", [row], stored=stored_state("2026-09-17", ("row1", "2026-09-17"))
		)

		keep_manual_due_date(invoice)
		set_due_date_from_payment_schedule(invoice)

		self.assertIsNone(invoice.payment_terms_template)
		self.assertEqual(row.due_date, "2026-09-17")
		self.assertEqual(row.payment_term, "Rimessa diretta 100%")
		self.assertEqual(row.credit_days, 15)
		self.assertEqual(str(invoice.due_date), "2026-09-17")


if __name__ == "__main__":
	unittest.main()
