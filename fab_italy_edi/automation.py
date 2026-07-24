from __future__ import annotations

from contextlib import contextmanager

import frappe


AUTOMATION_USER = "fab-edi-bot@example.invalid"
AUTOMATION_USER_NAME = "FAB EDI Bot"
AUTOMATION_USER_TYPE = "System User"
# covers Sales/Purchase Invoice reads, EDI Document writes and the private file
# access check that fires when attaching outbound payload artifacts
AUTOMATION_USER_ROLES = ("Accounts Manager",)


def ensure_automation_user() -> str:
	user_name = frappe.db.get_value("User", AUTOMATION_USER)
	if not user_name:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": AUTOMATION_USER,
				"first_name": AUTOMATION_USER_NAME,
				"enabled": 1,
				"user_type": AUTOMATION_USER_TYPE,
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
		ensure_automation_user_roles(user)
		return user.name

	user = frappe.get_doc("User", user_name)
	changed = False
	expected_fields = {
		"first_name": AUTOMATION_USER_NAME,
		"enabled": 1,
		"user_type": AUTOMATION_USER_TYPE,
		"send_welcome_email": 0,
	}
	for fieldname, expected_value in expected_fields.items():
		if user.get(fieldname) != expected_value:
			user.set(fieldname, expected_value)
			changed = True

	if changed:
		user.save(ignore_permissions=True)

	ensure_automation_user_roles(user)
	return user.name


def ensure_automation_user_roles(user) -> None:
	existing = {row.role for row in user.get("roles")}
	missing = [role for role in AUTOMATION_USER_ROLES if role not in existing]
	if not missing:
		return
	for role in missing:
		user.append("roles", {"role": role})
	user.save(ignore_permissions=True)


@contextmanager
def automation_user_context():
	current_user = getattr(getattr(frappe.local, "session", None), "user", None) or "Guest"
	automation_user = ensure_automation_user()
	if current_user == automation_user:
		yield automation_user
		return

	frappe.set_user(automation_user)
	try:
		yield automation_user
	finally:
		frappe.set_user(current_user)
