from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fab_italy_edi import automation


class TestAutomationUserContext(unittest.TestCase):
	def test_context_restores_previous_user(self):
		session = SimpleNamespace(user="Administrator")
		local = SimpleNamespace(session=session)
		set_user = Mock()

		with (
			patch.object(automation, "ensure_automation_user", return_value=automation.AUTOMATION_USER),
			patch.object(automation.frappe, "local", new=local),
			patch.object(automation.frappe, "set_user", new=set_user),
		):
			with automation.automation_user_context():
				pass

		self.assertEqual(
			[call.args[0] for call in set_user.call_args_list],
			[automation.AUTOMATION_USER, "Administrator"],
		)
