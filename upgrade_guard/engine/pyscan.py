"""AST helpers: name resolution through imports, and the python-aware rule kinds."""
import ast
import fnmatch
import re

SQLISH = re.compile(r"\w\s*\(|^\s*distinct\s|^\s*['\"]")


def collect_aliases(tree):
	aliases = {}
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for a in node.names:
				if a.asname:
					aliases[a.asname] = a.name
		elif isinstance(node, ast.ImportFrom) and node.module:
			for a in node.names:
				aliases[a.asname or a.name] = f"{node.module}.{a.name}"
	return aliases


def dotted(node, aliases):
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(aliases.get(node.id, node.id))
		return ".".join(reversed(parts))
	return None


def name_match(full, patterns):
	return full and any(fnmatch.fnmatchcase(full, p) for p in patterns)


def _src(lines, lineno):
	return lines[lineno - 1].strip()[:240] if 0 < lineno <= len(lines) else ""


def _has_sql_string(node):
	for n in ast.walk(node):
		if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQLISH.search(n.value):
			return n.value
	return None


def analyze(text, rules):
	"""Run python-aware rules.  Returns list of (rule, line, snippet, message) or None on syntax error."""
	try:
		tree = ast.parse(text)
	except (SyntaxError, ValueError, RecursionError, MemoryError):
		return None
	aliases = collect_aliases(tree)
	lines = text.splitlines()
	by_kind = {}
	for r in rules:
		by_kind.setdefault(r["kind"], []).append(r)
	out, seen = [], set()

	def emit(rule, lineno, message=None):
		key = (rule["id"], lineno)
		if key not in seen:
			seen.add(key)
			out.append((rule, lineno, _src(lines, lineno), message))

	name_rules = by_kind.get("py_name", [])
	call_rules = [r for k in ("py_call_kw", "py_call_no_kw", "py_call_sql_args", "py_call_dict_arg") for r in by_kind.get(k, [])]

	for node in ast.walk(tree):
		if name_rules:
			full = None
			if isinstance(node, ast.Attribute):
				full = dotted(node, aliases)
			elif isinstance(node, ast.Name) and node.id in aliases:
				full = aliases[node.id]
			if full:
				for r in name_rules:
					if name_match(full, r["names"]):
						emit(r, node.lineno)
			if isinstance(node, ast.ImportFrom) and node.module:
				for a in node.names:
					for r in name_rules:
						if name_match(f"{node.module}.{a.name}", r["names"]):
							emit(r, node.lineno)
			elif isinstance(node, ast.Import):
				for a in node.names:
					for r in name_rules:
						if name_match(a.name, r["names"]):
							emit(r, node.lineno)
		if isinstance(node, ast.Call) and call_rules:
			full = dotted(node.func, aliases)
			if not full:
				continue
			kws = {k.arg for k in node.keywords if k.arg}
			for r in call_rules:
				if not name_match(full, r["names"]):
					continue
				kind = r["kind"]
				if kind == "py_call_kw":
					kw = r["kwarg"]
					if kw == "*":
						hit = bool(kws) and len(node.args) >= r.get("min_args", 0)
					else:
						hit = kw in kws
					if hit and "value" in r:
						k = next((k for k in node.keywords if k.arg == kw), None)
						hit = bool(k and isinstance(k.value, ast.Constant) and k.value.value == r["value"])
					if hit:
						emit(r, node.lineno)
				elif kind == "py_call_no_kw":
					need = r.get("only_if_any_kwarg")
					if r["missing_kwarg"] not in kws and (not need or kws & set(need)):
						emit(r, node.lineno)
				elif kind == "py_call_dict_arg":
					if any(isinstance(a, ast.Dict) for a in node.args):
						emit(r, node.lineno)
				elif kind == "py_call_sql_args":
					for k in node.keywords:
						if k.arg in r["args"]:
							s = _has_sql_string(k.value)
							if s:
								emit(r, node.lineno, f"Raw SQL in `{k.arg}`: {s[:80]}")
							break

	for r in by_kind.get("py_setupclass", []):
		for node in ast.walk(tree):
			if isinstance(node, ast.ClassDef):
				for fn in node.body:
					if isinstance(fn, ast.FunctionDef) and fn.name == "setUpClass":
						calls_super = any(
							isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "setUpClass"
							for c in ast.walk(fn)
						)
						if not calls_super:
							emit(r, fn.lineno)
	return out


# ----- hooks.py helpers -------------------------------------------------------------------

def parse_hooks(path):
	"""Return {key: (value_or_None, lineno)} for top-level assignments of hooks.py."""
	try:
		with open(path, encoding="utf-8") as f:
			tree = ast.parse(f.read())
	except Exception:
		return {}
	hooks = {}
	for node in tree.body:
		if isinstance(node, ast.Assign):
			for t in node.targets:
				if isinstance(t, ast.Name):
					try:
						val = ast.literal_eval(node.value)
					except Exception:
						val = None
					hooks[t.id] = (val, node.lineno)
	return hooks


def iter_hook_paths(hook, value):
	"""Yield dotted function/class paths out of doc_events / has_permission / override_doctype."""
	if not isinstance(value, dict):
		return
	if hook == "doc_events":
		for _dt, events in value.items():
			if isinstance(events, dict):
				for _ev, paths in events.items():
					for p in ([paths] if isinstance(paths, str) else paths or []):
						if isinstance(p, str):
							yield p, _dt
	else:
		for dt, p in value.items():
			for q in ([p] if isinstance(p, str) else p or []):
				if isinstance(q, str):
					yield q, dt


_parse_cache = {}


def _parse_file(path):
	if path not in _parse_cache:
		try:
			with open(path, encoding="utf-8") as f:
				_parse_cache[path] = ast.parse(f.read())
		except Exception:
			_parse_cache[path] = None
	return _parse_cache[path]


def find_def(app_dirs, dotted_path):
	import os

	parts = dotted_path.split(".")
	if len(parts) < 2 or parts[0] not in app_dirs:
		return None
	base = os.path.join(app_dirs[parts[0]], *parts[1:-1]) if len(parts) > 2 else app_dirs[parts[0]]
	for f in (base + ".py", os.path.join(base, "__init__.py")):
		if os.path.isfile(f):
			tree = _parse_file(f)
			if tree:
				for n in tree.body:
					if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == parts[-1]:
						return f, n, tree
	return None


def check_hook_def(node, check, original_doctype=None):
	"""Return a message if the hooked function/class violates `check`, else None."""
	if check == "commit":
		for c in ast.walk(node):
			if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "commit":
				d = dotted(c.func, {})
				if d and d.endswith("db.commit"):
					return "calls frappe.db.commit()", c.lineno
	elif check == "returns_true" and isinstance(node, ast.FunctionDef):
		for c in ast.walk(node):
			if isinstance(c, ast.Return) and (c.value is None or (isinstance(c.value, ast.Constant) and c.value.value is None)):
				return "returns None / bare return — must return True explicitly", c.lineno
		last = node.body[-1] if node.body else None
		if not isinstance(last, (ast.Return, ast.Raise)):
			return "can fall through without returning True", node.lineno
	elif check == "extends_base" and isinstance(node, ast.ClassDef):
		bases = [dotted(b, {}) or "" for b in node.bases]
		bases = [b for b in bases if b not in ("", "object")]
		expect = (original_doctype or "").replace(" ", "").replace("-", "")
		if not bases:
			return "class has no base class — must extend the original controller", node.lineno
		if expect and not any(b.split(".")[-1] == expect for b in bases):
			return f"bases are {', '.join(bases)} — verify it extends the original {expect} controller", node.lineno
	return None
