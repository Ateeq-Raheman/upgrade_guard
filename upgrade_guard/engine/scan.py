"""Upgrade Guard scanner.  READ-ONLY: it never writes to the site database or to app files."""
import json
import os
import re
import shutil
import subprocess
import sys

import frappe

from upgrade_guard.engine import pyscan
from upgrade_guard.engine.constants import (
	EFFORT_HOURS,
	EXT_LANG,
	LANG_SETS,
	MAX_FILE_BYTES,
	MAX_FINDINGS_PER_RULE,
	OFFICIAL_APPS,
	SEVERITY_ORDER,
	SEVERITY_WEIGHT,
	SKIP_DIRS,
	VERSIONED_OFFICIAL,
)
from upgrade_guard.engine.rules import load_rules

try:
	import tomllib
except ImportError:  # pragma: no cover
	tomllib = None

AST_KINDS = {"py_name", "py_call_kw", "py_call_no_kw", "py_call_sql_args", "py_call_dict_arg", "py_setupclass"}
COMMENT_PREFIX = {"python": "#", "server_script": "#", "js": "//", "client_script": "//"}

# (doctype, filters, [(field, language)], name label)
DB_SOURCES = [
	("Server Script", {"disabled": 0}, [("script", "server_script")]),
	("Client Script", {"enabled": 1}, [("script", "client_script")]),
	("Custom HTML Block", {}, [("html", "html"), ("script", "client_script"), ("style", "css")]),
	("Print Format", {"disabled": 0, "standard": "No"}, [("html", "db_print"), ("css", "css")]),
	("Report", {"is_standard": "No"}, [("javascript", "client_script"), ("report_script", "server_script")]),
	("Web Form", {"is_standard": 0}, [("client_script", "client_script"), ("custom_css", "css")]),
	("Web Page", {}, [("main_section", "html"), ("context_script", "server_script"), ("javascript", "client_script"), ("css", "css")]),
	("Notification", {"enabled": 1, "is_standard": 0}, [("message", "html"), ("condition", "server_script"), ("subject", "html")]),
	("Email Template", {}, [("response", "html")]),
	("Workflow Transition", {}, [("condition", "server_script")]),
	("Assignment Rule", {}, [("assign_condition", "server_script"), ("unassign_condition", "server_script"), ("close_condition", "server_script")]),
	("Webhook", {"enabled": 1}, [("condition", "server_script"), ("webhook_json", "html")]),
]


def glob_match(rel, pattern):
	rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
	return re.fullmatch(rx, rel) is not None


def _ver_tuple(s):
	return tuple(int(x) for x in re.findall(r"\d+", s)[:3]) or (0,)


class Scanner:
	def __init__(self, source, target, apps=None, include_official=False, include_db=True, progress=None, max_findings=MAX_FINDINGS_PER_RULE):
		self.source, self.target = int(source), int(target)
		self.max_findings = max_findings
		self.include_official = include_official
		self.include_db = include_db
		self.progress = progress or (lambda pct, msg: None)
		self.rules, self.warnings = load_rules(self.source, self.target)
		self.by_kind = {}
		for r in self.rules:
			self.by_kind.setdefault(r["kind"], []).append(r)
		self.ast_rules = [r for r in self.rules if r["kind"] in AST_KINDS]
		self.regex_rules = self.by_kind.get("regex", [])
		for r in self.regex_rules:
			r["_rx"] = re.compile(r["pattern"])
		self.installed = frappe.get_installed_apps()
		self.app_dirs = {a: frappe.get_app_path(a) for a in self.installed}
		self.apps = apps or [a for a in self.installed if a != "upgrade_guard" and (include_official or a not in OFFICIAL_APPS)]
		self.findings, self.stats = [], {"files_scanned": 0, "records_scanned": 0, "truncated": {}}
		self._counts = {}
		self._seen = set()
		self._frm_calls, self._controllers = set(), []

	# ------------------------------------------------------------------ findings
	def add(self, rule, app, loc_type, ref, line=None, snippet="", message=None):
		key = (rule["id"], app, ref, line)
		if key in self._seen:
			return
		self._seen.add(key)
		n = self._counts[rule["id"]] = self._counts.get(rule["id"], 0) + 1
		if n > self.max_findings:
			self.stats["truncated"][rule["id"]] = self.stats["truncated"].get(rule["id"], 0) + 1
			return
		self.findings.append(
			{
				"rule_id": rule["id"],
				"title": rule["title"],
				"severity": rule["severity"],
				"confidence": rule.get("confidence", "high"),
				"category": rule["category"],
				"hop": rule["hop"],
				"app": app,
				"loc_type": loc_type,
				"reference": ref,
				"line": line or 0,
				"snippet": (snippet or "")[:300],
				"message": message or "",
				"fix": rule.get("fix", ""),
				"link": rule.get("link", ""),
				"autofixable": 1 if rule.get("autofix") else 0,
				"manual": 1 if rule["kind"] == "manual" else 0,
			}
		)

	# ------------------------------------------------------------------ text scanning
	def scan_text(self, text, lang, ref, app, loc_type="File", rel_path=None):
		langs = LANG_SETS.get(lang, {lang})
		prefix = next((COMMENT_PREFIX[l] for l in (lang,) if l in COMMENT_PREFIX), None)
		lines = text.splitlines()
		for r in self.regex_rules:
			if not langs & set(r["languages"]):
				continue
			if r.get("path_glob") and not (rel_path and any(glob_match(rel_path, g) for g in r["path_glob"])):
				continue
			rx = r["_rx"]
			for i, line in enumerate(lines, 1):
				if prefix and line.lstrip().startswith(prefix):
					continue
				m = rx.search(line)
				if m:
					snip = line.strip()
					if len(snip) > 240:
						snip = line[max(0, m.start() - 80): m.start() + 160].strip()
					self.add(r, app, loc_type, ref, i, snip)
		if langs & {"python", "server_script"} and self.ast_rules:
			res = pyscan.analyze(text, self.ast_rules)
			if res is None:
				self._fallback_names(text, lines, ref, app, loc_type)
			else:
				for rule, line, snip, msg in res:
					self.add(rule, app, loc_type, ref, line, snip, msg)
		if langs & {"js", "client_script"}:
			for m in re.finditer(r"frm\.call\(\s*[\"'](\w+)", text):
				self._frm_calls.add(m.group(1))

	def _fallback_names(self, text, lines, ref, app, loc_type):
		"""Syntax error in a script: fall back to plain name search so nothing is silently skipped."""
		for r in self.by_kind.get("py_name", []) + self.by_kind.get("py_call_kw", []):
			for n in r["names"]:
				if "*" in n:
					continue
				rx = re.compile(r"\b" + re.escape(n) + r"\b")
				for i, line in enumerate(lines, 1):
					if rx.search(line):
						self.add(r, app, loc_type, ref, i, line.strip(), "script has a syntax error; matched by text")

	# ------------------------------------------------------------------ file scanning
	def walk_app(self, app):
		pkg = self.app_dirs[app]
		root = os.path.dirname(pkg)
		rels = []
		for dp, dns, fns in os.walk(root):
			dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
			for fn in fns:
				rels.append(os.path.relpath(os.path.join(dp, fn), root))
		return root, pkg, rels

	def scan_app_files(self, app):
		root, pkg, rels = self.walk_app(app)
		hooks_path = os.path.join(pkg, "hooks.py")
		path_rules = self.by_kind.get("path_exists", [])
		for rel in rels:
			full = os.path.join(root, rel)
			ext = os.path.splitext(rel)[1].lower()
			for r in path_rules:
				if any(glob_match(rel, g) for g in r["path_glob"]) and (not r.get("app_root") or "/" not in rel):
					self.add(r, app, "File", rel, 0, rel)
			lang = EXT_LANG.get(ext)
			if not lang or rel.endswith(".min.js"):
				continue
			try:
				if os.path.getsize(full) > MAX_FILE_BYTES:
					continue
				with open(full, encoding="utf-8", errors="ignore") as f:
					text = f.read()
			except OSError:
				continue
			self.stats["files_scanned"] += 1
			self.scan_text(text, lang, rel, app, "File", rel)
			if lang == "json":
				self.scan_json_file(text, rel, app)
			if lang == "python" and re.search(r"[\\/]doctype[\\/][^\\/]+[\\/][^\\/]+\.py$", rel):
				self._controllers.append((app, rel, full))
		self.scan_hooks(app, hooks_path)
		self.scan_pyproject(app, root)

	def scan_json_file(self, text, rel, app):
		try:
			doc = json.loads(text)
		except ValueError:
			return
		docs = doc if isinstance(doc, list) else [doc]
		for d in docs:
			if not isinstance(d, dict):
				continue
			dt = d.get("doctype")
			for r in self.by_kind.get("doctype_json", []):
				if dt == "DocType" and all(d.get(k) == v for k, v in r["match"].items()):
					self.add(r, app, "File", rel, 0, ", ".join(f"{k}={v}" for k, v in r["match"].items()))
			for r in self.by_kind.get("json_has_keys", []):
				if glob_match(rel, r["path_glob"][0]) or any(glob_match(rel, g) for g in r["path_glob"]):
					hit = [k for k in r["keys"] if k in d]
					if hit:
						self.add(r, app, "File", rel, 0, "keys: " + ", ".join(hit))
			for r in self.by_kind.get("json_missing_keys", []):
				if dt == "Workspace" and any(glob_match(rel, g) for g in r["path_glob"]):
					miss = [k for k in r["keys"] if k not in d]
					if miss:
						self.add(r, app, "File", rel, 0, "missing: " + ", ".join(miss))
			if dt == "Print Format":
				self.check_print_format(d, app, "File", rel)
			for r in self.by_kind.get("db_filters_json", []):
				if self._bad_filters(d.get("filters_json")):
					self.add(r, app, "File", rel, 0, str(d.get("filters_json"))[:200])

	@staticmethod
	def _bad_filters(raw):
		if not raw or not isinstance(raw, str):
			return False
		try:
			data = json.loads(raw)
		except ValueError:
			return False
		return isinstance(data, list) and any(isinstance(f, list) and len(f) == 5 for f in data)

	def check_print_format(self, d, app, loc_type, ref):
		for r in self.by_kind.get("db_print_classic", []):
			if d.get("custom_format") and not d.get("print_format_builder_beta") and d.get("print_format_type", "Jinja") == "Jinja" and not d.get("disabled"):
				self.add(r, app, loc_type, ref, 0, f"Classic print format for {d.get('doc_type')}")

	def scan_hooks(self, app, hooks_path):
		if not os.path.isfile(hooks_path):
			return
		rel = f"{app}/hooks.py"
		hooks = pyscan.parse_hooks(hooks_path)
		for r in self.by_kind.get("hook_key", []):
			if r["key"] in hooks:
				self.add(r, app, "File", rel, hooks[r["key"]][1], f"{r['key']} is defined")
		for r in self.by_kind.get("hook_missing", []):
			if r["key"] not in hooks and (not r.get("custom_apps_only") or app not in OFFICIAL_APPS):
				self.add(r, app, "File", rel, 0, f"hooks.py has no `{r['key']}`")
		for r in self.by_kind.get("hook_value_regex", []):
			rx = re.compile(r["pattern"])
			for k in r["keys"]:
				val = hooks.get(k, (None, 0))
				for v in ([val[0]] if isinstance(val[0], str) else val[0] or []):
					if isinstance(v, str) and rx.search(v):
						self.add(r, app, "File", rel, val[1], f"{k}: {v}")
		for r in self.by_kind.get("hook_funcs", []):
			val = hooks.get(r["hook"], (None, 0))[0]
			for path, dt in pyscan.iter_hook_paths(r["hook"], val):
				found = pyscan.find_def(self.app_dirs, path)
				if not found:
					continue
				fpath, node, _tree = found
				res = pyscan.check_hook_def(node, r["check"], dt)
				if res:
					msg, line = res
					self.add(r, app, "File", os.path.relpath(fpath, os.path.dirname(self.app_dirs[app])) if fpath.startswith(os.path.dirname(self.app_dirs[app])) else fpath, line, f"{path} ({dt}): {msg}", msg)

	def scan_pyproject(self, app, root):
		path = os.path.join(root, "pyproject.toml")
		if not (tomllib and os.path.isfile(path)):
			return
		try:
			with open(path, "rb") as f:
				data = tomllib.load(f)
		except Exception:
			return
		spec = (((data.get("tool") or {}).get("bench") or {}).get("frappe-dependencies") or {}).get("frappe")
		if not spec:
			return
		m = re.search(r"<\s*=?\s*(\d+)", spec)
		for r in self.by_kind.get("pyproject_frappe_cap", []):
			if m and int(m.group(1)) <= self.target:
				self.add(r, app, "File", "pyproject.toml", 0, f'frappe = "{spec}"')

	def check_doc_methods(self):
		"""v13: methods called from the client via frm.call() must be @frappe.whitelist()."""
		rules = self.by_kind.get("doc_methods_whitelist", [])
		if not rules or not self._frm_calls:
			return
		import ast

		for app, rel, full in self._controllers:
			try:
				with open(full, encoding="utf-8") as f:
					tree = ast.parse(f.read())
			except Exception:
				continue
			for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
				for fn in cls.body:
					if isinstance(fn, ast.FunctionDef) and fn.name in self._frm_calls:
						decos = " ".join(ast.dump(d) for d in fn.decorator_list)
						if "whitelist" not in decos:
							self.add(rules[0], app, "File", rel, fn.lineno, f"def {fn.name}() is called via frm.call but not whitelisted")

	# ------------------------------------------------------------------ environment
	def scan_environment(self):
		info = {"python": sys.version.split()[0]}
		try:
			info["node"] = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
		except Exception:
			info["node"] = ""
		try:
			info["database"] = frappe.db.sql("select version()")[0][0]
		except Exception:
			info["database"] = ""
		info["apps"] = {}
		for a in self.installed:
			try:
				info["apps"][a] = frappe.get_attr(a + ".__version__")
			except Exception:
				info["apps"][a] = ""
		self.stats["environment"] = info
		for r in self.by_kind.get("env", []):
			for key in ("python", "node"):
				need = r.get(f"{key}_min")
				have = info.get(key)
				if need and have and _ver_tuple(have) < _ver_tuple(need):
					self.add(r, "(environment)", "Environment", f"{key} {have}", 0, f"Installed {key} {have} < required {need}")
		for r in self.by_kind.get("env_chromium", []):
			conf = frappe.get_conf()
			if not (conf.get("chromium_path") or shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chrome-headless-shell")):
				self.add(r, "(environment)", "Environment", "chromium", 0, "No Chromium found and chromium_path not configured")
		for r in self.by_kind.get("env_server_script", []):
			if not frappe.get_conf().get("server_script_enabled"):
				try:
					n = frappe.db.count("Server Script")
				except Exception:
					n = 0
				if n:
					self.add(r, "(site)", "Environment", "server_script_enabled", 0, f"{n} Server Scripts exist but server_script_enabled is off")
		# official-app branch requirement
		for a in self.installed:
			if a in VERSIONED_OFFICIAL and a in info["apps"] and info["apps"][a]:
				major = _ver_tuple(info["apps"][a])[0]
				if major < self.target:
					self.add_internal("UG-001", f"{a} must be switched to branch version-{self.target}", "high", a, f"{a} is {info['apps'][a]}", f"git switch / bench switch-to-branch version-{self.target} {a}")
			elif a in OFFICIAL_APPS and a not in VERSIONED_OFFICIAL:
				self.add_internal("UG-002", f"Verify {a} supports Frappe v{self.target}", "info", a, f"{a} {info['apps'].get(a, '')}", "Official app with its own release line; check its README/branches for Frappe compatibility.")
			elif a not in OFFICIAL_APPS and a in self.apps:
				self.add_internal("UG-003", f"Third-party/custom app {a}: confirm a v{self.target}-compatible branch", "info", a, a, "Run the scan results for this app and test on the staging site.")

	def add_internal(self, rid, title, sev, app, snippet, fix):
		rule = {"id": rid, "title": title, "severity": sev, "category": "compatibility", "hop": f"{self.source}→{self.target}", "kind": "internal", "fix": fix, "confidence": "high"}
		self.add(rule, app, "Environment", app, 0, snippet)

	# ------------------------------------------------------------------ database scanning
	def scan_db(self):
		total = len(DB_SOURCES) + 6
		for i, (dt, filters, fields) in enumerate(DB_SOURCES):
			self.progress(40 + int(45 * i / total), f"Scanning {dt} records")
			try:
				names = [f for f, _ in fields]
				try:
					rows = frappe.get_all(dt, filters=filters, fields=["name"] + names, limit_page_length=0)
				except Exception:
					# older versions may lack the is_standard/standard column: retry with the base filters
					base = {k: v for k, v in filters.items() if k not in ("is_standard", "standard")}
					rows = frappe.get_all(dt, filters=base, fields=["name"] + names, limit_page_length=0)
			except Exception:
				continue  # doctype/column does not exist on this version
			for row in rows:
				self.stats["records_scanned"] += 1
				for field, lang in fields:
					text = row.get(field)
					if text and isinstance(text, str):
						self.scan_text(text, lang, f"{dt}: {row['name']} [{field}]", "(site customization)", "Record")
				if dt == "Print Format":
					pass
			if dt == "Print Format" and self.by_kind.get("db_print_classic"):
				self._db_classic_print()
		self._db_doctype_sort()
		self._db_rating_fields()
		self._db_filters_json()

	def _db_classic_print(self):
		try:
			rows = frappe.get_all("Print Format", filters={"custom_format": 1, "disabled": 0, "standard": "No", "print_format_builder_beta": 0}, fields=["name", "doc_type", "print_format_type"], limit_page_length=0)
		except Exception:
			return
		for row in rows:
			if row.get("print_format_type", "Jinja") == "Jinja":
				self.check_print_format({"custom_format": 1, "doc_type": row["doc_type"]}, "(site customization)", "Record", f"Print Format: {row['name']}")

	def _db_doctype_sort(self):
		for r in self.by_kind.get("doctype_json", []):
			for k, v in r["match"].items():
				try:
					for row in frappe.get_all("DocType", filters={"custom": 1, k: v}, fields=["name"]):
						self.add(r, "(site customization)", "Record", f"DocType: {row.name}", 0, f"{k}={v}")
					for row in frappe.get_all("Property Setter", filters={"property": k, "value": v}, fields=["doc_type", "name"]):
						self.add(r, "(site customization)", "Record", f"Property Setter: {row.name}", 0, f"{row.doc_type}: {k}={v}")
				except Exception:
					pass

	def _db_rating_fields(self):
		for r in self.by_kind.get("db_rating_fields", []):
			try:
				for row in frappe.get_all("Custom Field", filters={"fieldtype": "Rating"}, fields=["name", "dt", "fieldname"]):
					self.add(r, "(site customization)", "Record", f"Custom Field: {row.name}", 0, f"{row.dt}.{row.fieldname} is a Rating field")
				mods = {m for a in self.apps for m in (frappe.get_all("Module Def", filters={"app_name": a}, pluck="name") or [])}
				if mods:
					for row in frappe.db.sql("select f.parent, f.fieldname from `tabDocField` f join `tabDocType` d on d.name=f.parent where f.fieldtype='Rating' and d.module in %s", (tuple(mods),), as_dict=True):
						self.add(r, "(site customization)", "Record", f"DocType: {row.parent}", 0, f"{row.parent}.{row.fieldname} is a Rating field")
			except Exception:
				pass

	def _db_filters_json(self):
		for r in self.by_kind.get("db_filters_json", []):
			for dt in ("Dashboard Chart", "Number Card"):
				try:
					for row in frappe.get_all(dt, filters={"is_standard": 0}, fields=["name", "filters_json"], limit_page_length=0):
						if self._bad_filters(row.filters_json):
							self.add(r, "(site customization)", "Record", f"{dt}: {row.name}", 0, str(row.filters_json)[:200])
				except Exception:
					pass

	def add_manual(self):
		for r in self.by_kind.get("manual", []):
			self.add(r, "(manual check)", "Manual", "Verify before upgrading", 0, r["title"])

	# ------------------------------------------------------------------ run
	def run(self):
		self.progress(2, "Loading rules")
		self.scan_environment()
		for i, app in enumerate(self.apps):
			self.progress(8 + int(30 * i / max(len(self.apps), 1)), f"Scanning app {app}")
			if app in self.app_dirs:
				self.scan_app_files(app)
		self.check_doc_methods()
		if self.include_db:
			self.scan_db()
		self.add_manual()
		self.progress(90, "Computing verdict")
		return self.result()

	# ------------------------------------------------------------------ result
	def result(self):
		sev_counts = {s: 0 for s in SEVERITY_ORDER}
		per_app = {}
		score_penalty, hours = 0.0, 0.0
		effective = {s: 0 for s in SEVERITY_ORDER}
		for f in self.findings:
			sev_counts[f["severity"]] += 1
			pa = per_app.setdefault(f["app"], {s: 0 for s in SEVERITY_ORDER})
			pa[f["severity"]] += 1
			if f["manual"] or (f["app"] in OFFICIAL_APPS and f["rule_id"] != "UG-001"):
				continue  # official-app source is informational: you cannot fix it, you upgrade it
			w = SEVERITY_WEIGHT[f["severity"]] * (0.4 if f["confidence"] == "low" else 1)
			score_penalty += w
			hours += EFFORT_HOURS[f["severity"]] * (0.2 if f["autofixable"] else 1) * (0.5 if f["confidence"] == "low" else 1)
			eff = f["severity"]
			if f["confidence"] == "low" and eff in ("blocker", "high", "medium"):
				eff = SEVERITY_ORDER[SEVERITY_ORDER.index(eff) + 1]
			effective[eff] += 1
		if effective["blocker"]:
			verdict = "NO-GO"
		elif effective["high"] or effective["medium"]:
			verdict = "GO WITH FIXES"
		else:
			verdict = "GO"
		self.findings.sort(key=lambda f: (SEVERITY_ORDER.index(f["severity"]), f["app"], f["reference"], f["line"]))
		return {
			"source": self.source,
			"target": self.target,
			"verdict": verdict,
			"score": round(100 / (1 + score_penalty / 60), 1),
			"estimated_hours": round(hours, 1),
			"severity_counts": sev_counts,
			"per_app": per_app,
			"findings": self.findings,
			"stats": self.stats,
			"warnings": self.warnings,
			"rules_loaded": len(self.rules),
			"apps_scanned": self.apps,
		}


def detect_source_version():
	return int(frappe.__version__.split(".")[0])


def run_scan(source=None, target=16, **kw):
	source = source or detect_source_version()
	return Scanner(source, target, **kw).run()
