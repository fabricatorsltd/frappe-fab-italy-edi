from __future__ import annotations

import unittest
from types import SimpleNamespace

from fab_italy_edi.edi_lifecycle import (
	RECEIPT_STATE_PROGRESS,
	TRANSMISSION_STATE_PROGRESS,
	append_edi_receipt,
	find_receipt_row,
	reconcile_edi_document_states,
	should_apply_state_transition,
)


class TestEDILifecycle(unittest.TestCase):
	def test_failed_document_can_reenter_queue(self):
		self.assertTrue(
			should_apply_state_transition(
				"failed",
				"queued",
				progress_order=TRANSMISSION_STATE_PROGRESS,
				reopenable_transitions={("failed", "queued")},
			)
		)

	def test_terminal_receipt_state_does_not_downgrade(self):
		self.assertFalse(
			should_apply_state_transition(
				"accepted",
				"delivered",
				progress_order=RECEIPT_STATE_PROGRESS,
			)
		)

	def test_append_edi_receipt_is_idempotent(self):
		document = SimpleNamespace(receipts=[])
		document.append = lambda fieldname, value: getattr(document, fieldname).append(SimpleNamespace(**value))

		created = append_edi_receipt(
			document,
			receipt_type="RC",
			normalized_state="delivered",
			external_message_id="ntf-123",
			processing_notes="type: RC",
		)
		created_again = append_edi_receipt(
			document,
			receipt_type="RC",
			normalized_state="delivered",
			external_message_id="ntf-123",
			processing_notes="type: RC",
		)

		self.assertTrue(created)
		self.assertFalse(created_again)
		self.assertEqual(len(document.receipts), 1)
		self.assertIsNotNone(find_receipt_row(document, "ntf-123", receipt_type="RC"))

	def test_reconcile_edi_document_states_returns_activity_message(self):
		document = SimpleNamespace(
			transmission_state="queued",
			latest_receipt_state="queued",
			external_submission_id="uuid-123",
			canonical_identifier=None,
			last_error=None,
		)

		changed, message = reconcile_edi_document_states(
			document,
			transmission_state="delivered",
			receipt_state="delivered",
			event_label="received proxy update",
			detail_text="marking: Ricevuta di consegna",
		)

		self.assertTrue(changed)
		self.assertEqual(document.transmission_state, "delivered")
		self.assertEqual(document.latest_receipt_state, "delivered")
		self.assertIn("received proxy update", message)
