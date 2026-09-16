import unittest

from fab_italy_edi.patches.v16 import name_autofatture_by_series as naming_patch


class TestAutofatturaRenamePlan(unittest.TestCase):
	def test_hashed_names_are_numbered_per_year_in_creation_order(self):
		plan = naming_patch.build_rename_plan(
			[
				("74algc3kkp", 2025),
				("8xk2p0qw1z", 2025),
				("q9r4t7v2ba", 2026),
			]
		)

		self.assertEqual(
			plan.renames,
			[
				("74algc3kkp", "AUTOFATT/2025/00001"),
				("8xk2p0qw1z", "AUTOFATT/2025/00002"),
				("q9r4t7v2ba", "AUTOFATT/2026/00001"),
			],
		)
		self.assertEqual(plan.counters, {2025: 2, 2026: 1})

	def test_records_already_in_the_series_are_skipped_and_seed_the_counter(self):
		plan = naming_patch.build_rename_plan(
			[
				("AUTOFATT/2026/00001", 2026),
				("AUTOFATT/2026/00007", 2026),
				("74algc3kkp", 2026),
			]
		)

		self.assertEqual(plan.renames, [("74algc3kkp", "AUTOFATT/2026/00008")])
		self.assertEqual(plan.counters, {2026: 8})

	def test_plan_is_empty_when_every_record_carries_a_series_name(self):
		plan = naming_patch.build_rename_plan([("AUTOFATT/2026/00001", 2026)])

		self.assertEqual(plan.renames, [])
		self.assertEqual(plan.counters, {2026: 1})

	def test_series_name_parsing_ignores_foreign_formats(self):
		self.assertEqual(naming_patch.parse_series_name("AUTOFATT/2026/00012"), (2026, 12))
		self.assertIsNone(naming_patch.parse_series_name("74algc3kkp"))
		self.assertIsNone(naming_patch.parse_series_name("AUTOFATT/26/00012"))
		self.assertIsNone(naming_patch.parse_series_name(None))

	def test_series_prefix_matches_the_naming_series_key(self):
		self.assertEqual(naming_patch.get_series_prefix(2026), "AUTOFATT/2026/")
		self.assertEqual(naming_patch.format_series_name(2026, 9), "AUTOFATT/2026/00009")
