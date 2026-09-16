from __future__ import annotations

from dataclasses import dataclass, field
import re

import frappe
from frappe.query_builder import DocType
from frappe.utils import cint, cstr, getdate


AUTOFATTURA_NAMING_SERIES = "AUTOFATT/.YYYY./.#####"
AUTOFATTURA_NAME_PATTERN = re.compile(r"^AUTOFATT/(\d{4})/(\d+)$")
AUTOFATTURA_NAME_DIGITS = 5


@dataclass(slots=True)
class AutofatturaRenamePlan:
	renames: list[tuple[str, str]] = field(default_factory=list)
	counters: dict[int, int] = field(default_factory=dict)


def execute():
	"""Give hashed autofatture an Italian series name and keep the yearly counter aligned."""
	if not frappe.db.table_exists("Autofattura"):
		return

	rows = frappe.get_all(
		"Autofattura",
		fields=["name", "creation", "naming_series", "document_naming_series"],
		order_by="creation asc, name asc",
	)
	if not rows:
		return

	plan = build_rename_plan([(row.name, getdate(row.creation).year) for row in rows])
	document_series = {
		row.name: cstr(row.naming_series).strip()
		for row in rows
		if not cstr(row.document_naming_series).strip()
	}

	for old_name, new_name in plan.renames:
		frappe.rename_doc(
			"Autofattura",
			old_name,
			new_name,
			force=True,
			show_alert=False,
			rebuild_search=False,
		)
		carry_over_document_naming_series(new_name, document_series.get(old_name))
		refresh_transport_idempotency_key(new_name)

	set_naming_series_field()
	for year, current in plan.counters.items():
		reserve_series(year, current)


def build_rename_plan(records: list[tuple[str, int]]) -> AutofatturaRenamePlan:
	"""Number the records that still carry a hashed name, per year, in the given order."""
	plan = AutofatturaRenamePlan()
	for name, _creation_year in records:
		parsed = parse_series_name(name)
		if parsed:
			year, number = parsed
			plan.counters[year] = max(plan.counters.get(year, 0), number)

	for name, creation_year in records:
		if parse_series_name(name):
			continue
		plan.counters[creation_year] = plan.counters.get(creation_year, 0) + 1
		plan.renames.append((name, format_series_name(creation_year, plan.counters[creation_year])))

	return plan


def parse_series_name(name: str | None) -> tuple[int, int] | None:
	match = AUTOFATTURA_NAME_PATTERN.match(cstr(name).strip())
	if not match:
		return None
	return int(match.group(1)), int(match.group(2))


def format_series_name(year: int, number: int) -> str:
	return f"AUTOFATT/{year}/{number:0{AUTOFATTURA_NAME_DIGITS}d}"


def get_series_prefix(year: int) -> str:
	return f"AUTOFATT/{year}/"


def carry_over_document_naming_series(name: str, series: str | None) -> None:
	"""Keep the series the autofattura number is drawn from, now that the field moved."""
	if not series:
		return
	frappe.db.set_value("Autofattura", name, "document_naming_series", series, update_modified=False)


def refresh_transport_idempotency_key(name: str) -> None:
	for document in frappe.get_all(
		"EDI Document",
		filters={"source_doctype": "Autofattura", "source_name": name},
		pluck="name",
	):
		frappe.db.set_value(
			"EDI Document",
			document,
			"idempotency_key",
			f"autofattura:{name}",
			update_modified=False,
		)


def set_naming_series_field() -> None:
	frappe.db.sql(
		"update `tabAutofattura` set `naming_series` = %s where ifnull(`naming_series`, '') != %s",
		(AUTOFATTURA_NAMING_SERIES, AUTOFATTURA_NAMING_SERIES),
	)


def reserve_series(year: int, current: int) -> None:
	prefix = get_series_prefix(year)
	series = DocType("Series")
	rows = (frappe.qb.from_(series).where(series.name == prefix).for_update().select("current")).run()
	if not rows:
		frappe.db.sql("insert into `tabSeries` (`name`, `current`) values (%s, %s)", (prefix, current))
		return
	if cint(rows[0][0]) < current:
		frappe.db.sql("update `tabSeries` set `current` = %s where `name` = %s", (current, prefix))
