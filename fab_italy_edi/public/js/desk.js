frappe.provide("fab_italy_edi");

(() => {
	const AUTOFATTURA_WORKSPACE_STORAGE_KEY = "fab_italy_edi.autofattura_workspace";

	function normalize_sidebar_name(value) {
		if (!value || typeof value !== "string") {
			return value || null;
		}

		try {
			const parsed = JSON.parse(value);
			return typeof parsed === "string" ? parsed : value;
		} catch {
			return value;
		}
	}

	function get_sidebar_from_route_context() {
		return normalize_sidebar_name(
			frappe.route_options?.sidebar ||
				new URLSearchParams(window.location.search).get("sidebar") ||
				window.sessionStorage?.getItem(AUTOFATTURA_WORKSPACE_STORAGE_KEY)
		);
	}

	fab_italy_edi.remember_autofattura_sidebar = function () {
		const sidebar = frappe.app?.sidebar?.sidebar_title;
		if (!sidebar) {
			return;
		}

		window.sessionStorage?.setItem(AUTOFATTURA_WORKSPACE_STORAGE_KEY, sidebar);
		frappe.route_options = {
			...(frappe.route_options || {}),
			sidebar,
		};
	};

	fab_italy_edi.get_autofattura_sidebar = get_sidebar_from_route_context;

	fab_italy_edi.apply_autofattura_sidebar = function () {
		const route = frappe.get_route?.() || [];
		if (route[0] !== "Form" || route[1] !== "Autofattura") {
			return;
		}

		const sidebar = get_sidebar_from_route_context();
		if (!sidebar) {
			return;
		}

		frappe.route_options = {
			...(frappe.route_options || {}),
			sidebar,
		};
	};

	if (frappe.router && !fab_italy_edi._autofattura_sidebar_patch_applied) {
		const original_set_route_options_from_url = frappe.router.set_route_options_from_url.bind(
			frappe.router
		);
		frappe.router.set_route_options_from_url = function (...args) {
			original_set_route_options_from_url(...args);
			fab_italy_edi.apply_autofattura_sidebar();
		};
		fab_italy_edi._autofattura_sidebar_patch_applied = true;
	}
})();
