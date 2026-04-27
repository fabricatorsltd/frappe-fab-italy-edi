from frappe import _


def get_data():
	return [
		{
			"label": _("Configuration"),
			"icon": "fa fa-cog",
			"items": [
				{
					"type": "doctype",
					"name": "EDI Channel",
					"description": _("Channel families such as SDI proxy or PEC."),
				},
				{
					"type": "doctype",
					"name": "EDI Provider",
					"description": _("Provider adapters and credential-bearing endpoints."),
				},
				{
					"type": "doctype",
					"name": "EDI Configuration",
					"description": _("Company-specific transport and sender settings."),
				},
			],
		},
		{
			"label": _("Operations"),
			"icon": "fa fa-exchange",
			"items": [
				{
					"type": "doctype",
					"name": "EDI Document",
					"description": _("Operational record for outbound and inbound EDI flows."),
				}
			],
		},
	]
