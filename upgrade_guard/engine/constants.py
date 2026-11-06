# Apps that ship from the Frappe org and follow their own release branches.
VERSIONED_OFFICIAL = {"frappe", "erpnext", "hrms", "education", "healthcare", "webshop"}
OFFICIAL_APPS = VERSIONED_OFFICIAL | {
	"crm", "helpdesk", "lending", "payments", "telephony", "insights", "builder", "drive",
	"wiki", "gameplan", "lms", "print_designer", "newsletter", "blog", "eps", "offsite_backups",
	"frappe_whatsapp", "raven",
}

SKIP_DIRS = {
	"node_modules", ".git", "__pycache__", "dist", ".yarn", ".venv", "venv", "locale",
	"lib", "libs", "vendor", "build", ".github", "docs",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FINDINGS_PER_RULE = 200

EXT_LANG = {
	".py": "python", ".js": "js", ".vue": "js", ".ts": "js", ".jsx": "js", ".tsx": "js",
	".html": "html", ".jinja": "html", ".jinja2": "html",
	".css": "css", ".scss": "css", ".less": "css",
	".json": "json",
	".yml": "text", ".yaml": "text", ".sh": "text", ".toml": "text", ".cfg": "text", ".txt": "text",
}

# language of a scanned unit -> set of rule languages that apply to it
LANG_SETS = {
	"python": {"python"},
	"server_script": {"server_script", "python"},
	"js": {"js"},
	"client_script": {"client_script", "js"},
	"html": {"html"},
	"db_print": {"db_print", "html"},
	"css": {"css"},
	"json": {"json"},
	"text": {"text"},
}

SEVERITY_ORDER = ["blocker", "high", "medium", "low", "info"]
SEVERITY_WEIGHT = {"blocker": 10, "high": 4, "medium": 1.5, "low": 0.3, "info": 0}
EFFORT_HOURS = {"blocker": 1.0, "high": 0.5, "medium": 0.25, "low": 0.05, "info": 0}
