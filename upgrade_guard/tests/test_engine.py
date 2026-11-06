"""Unit tests for the pure parts of the engine (no site or database needed)."""
import json
import os
import re
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(os.path.dirname(HERE), "rules")
VALID_KINDS = {
	"env", "hook_missing", "regex", "hook_key", "hook_funcs", "py_name", "py_call_kw", "py_call_no_kw",
	"py_call_sql_args", "py_call_dict_arg", "py_setupclass", "doctype_json", "manual", "db_print_classic",
	"env_chromium", "pyproject_frappe_cap", "path_exists", "hook_value_regex", "json_has_keys",
	"json_missing_keys", "db_rating_fields", "env_server_script", "doc_methods_whitelist", "db_filters_json",
}
SEVERITIES = {"blocker", "high", "medium", "low", "info"}


def all_rules():
	for fn in sorted(os.listdir(RULES)):
		if fn.endswith(".yaml"):
			with open(os.path.join(RULES, fn), encoding="utf-8") as f:
				pack = yaml.safe_load(f)
			for r in pack["rules"]:
				yield fn, pack, r


class TestRulePacks(unittest.TestCase):
	def test_ids_unique_and_well_formed(self):
		seen = set()
		for fn, pack, r in all_rules():
			self.assertNotIn(r["id"], seen, f"duplicate id {r['id']}")
			seen.add(r["id"])
			self.assertIn(r["kind"], VALID_KINDS, f"{r['id']}: unknown kind {r['kind']}")
			self.assertIn(r["severity"], SEVERITIES, r["id"])
			self.assertTrue(r.get("title"), r["id"])
			self.assertTrue(r.get("fix") or r["kind"] == "manual", f"{r['id']} needs a fix hint")
			self.assertTrue(r["id"].startswith(f"V{pack['hop']['to']}-"), f"{r['id']} in wrong pack {fn}")

	def test_kind_specific_fields_and_patterns_compile(self):
		for _fn, _p, r in all_rules():
			k = r["kind"]
			if k == "regex":
				re.compile(r["pattern"])
			if k in ("py_name", "py_call_kw", "py_call_no_kw", "py_call_sql_args", "py_call_dict_arg"):
				self.assertTrue(r.get("names"), r["id"])
			if k == "py_call_kw":
				self.assertIn("kwarg", r, r["id"])
			if k == "py_call_no_kw":
				self.assertIn("missing_kwarg", r, r["id"])
			for rep in (r.get("autofix") or {}).get("replace", []):
				re.compile(rep["pattern"])
				if rep.get("unless"):
					re.compile(rep["unless"])

	def test_every_hop_is_chained(self):
		hops = set()
		for _fn, pack, _r in all_rules():
			hops.add((pack["hop"]["from"], pack["hop"]["to"]))
		for a in range(12, 16):
			self.assertIn((a, a + 1), hops)


class TestPyScan(unittest.TestCase):
	def setUp(self):
		from upgrade_guard.engine import pyscan

		self.pyscan = pyscan
		R = lambda **k: dict(severity="high", category="x", **k)  # noqa: E731
		self.rules = [
			R(id="A", kind="py_name", names=["frappe.db.set", "frappe.translate.get_dict"]),
			R(id="B", kind="py_call_kw", names=["frappe.sendmail"], kwarg="now"),
			R(id="C", kind="py_call_no_kw", names=["frappe.get_all"], missing_kwarg="order_by", only_if_any_kwarg=["limit"]),
			R(id="D", kind="py_call_sql_args", names=["frappe.get_all"], args=["fields"]),
			R(id="E", kind="py_setupclass"),
			R(id="F", kind="py_call_dict_arg", names=["frappe.db.exists"]),
		]

	def hits(self, code):
		res = self.pyscan.analyze(code, self.rules)
		self.assertIsNotNone(res)
		return sorted((r["id"], line) for r, line, _s, _m in res)

	def test_name_via_import_alias(self):
		code = "from frappe.translate import get_dict as gd\nx = gd('de')\nfrappe.db.set(a, b)\n"
		self.assertEqual(self.hits(code), [("A", 1), ("A", 2), ("A", 3)])

	def test_comments_and_strings_do_not_match(self):
		self.assertEqual(self.hits("# frappe.db.set(x)\ns = 'frappe.db.set'\n"), [])

	def test_kwarg_rules(self):
		code = "frappe.sendmail(a, now=True)\nfrappe.sendmail(a)\n"
		self.assertEqual(self.hits(code), [("B", 1)])

	def test_missing_order_by_only_with_limit(self):
		code = "frappe.get_all('X', limit=1)\nfrappe.get_all('X')\nfrappe.get_all('X', limit=1, order_by='creation')\n"
		self.assertEqual(self.hits(code), [("C", 1)])

	def test_sql_in_fields(self):
		code = "frappe.get_all('X', fields=['count(name) as c'])\nfrappe.get_all('X', fields=['name', 'title'])\n"
		self.assertEqual(self.hits(code), [("D", 1)])

	def test_setupclass_super(self):
		bad = "class T:\n    @classmethod\n    def setUpClass(cls):\n        x = 1\n"
		good = "class T:\n    @classmethod\n    def setUpClass(cls):\n        super().setUpClass()\n"
		self.assertEqual(self.hits(bad), [("E", 3)])
		self.assertEqual(self.hits(good), [])

	def test_exists_with_dict(self):
		self.assertEqual(self.hits("frappe.db.exists('X', {'a': 1})\nfrappe.db.exists('X', 'n')\n"), [("F", 1)])

	def test_syntax_error_returns_none(self):
		self.assertIsNone(self.pyscan.analyze("def (:", self.rules))

	def test_hook_checks(self):
		import ast

		fn = ast.parse("def f(doc):\n    frappe.db.commit()\n").body[0]
		self.assertIn("commit", self.pyscan.check_hook_def(fn, "commit")[0])
		perm = ast.parse("def p(doc):\n    if x:\n        return True\n").body[0]
		self.assertIsNotNone(self.pyscan.check_hook_def(perm, "returns_true"))
		ok = ast.parse("def p(doc):\n    return True\n").body[0]
		self.assertIsNone(self.pyscan.check_hook_def(ok, "returns_true"))
		cls = ast.parse("class SalesInvoice:\n    pass\n").body[0]
		self.assertIn("no base", self.pyscan.check_hook_def(cls, "extends_base", "Sales Invoice")[0])


class TestFixer(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		try:
			from upgrade_guard.engine import fixer
		except ImportError:  # frappe not installed in this environment
			raise unittest.SkipTest("frappe not available")
		cls.fixer = fixer
		with open(os.path.join(RULES, "v15_to_v16.yaml"), encoding="utf-8") as f:
			cls.rules = {r["id"]: r for r in yaml.safe_load(f)["rules"]}

	def fix(self, rid, text, line=1):
		used = set()
		return self.fixer._apply(text, line, self.rules[rid]["autofix"], rid, used)

	def test_in_test_reads_fixed_writes_untouched(self):
		self.assertEqual(self.fix("V16-020", "if frappe.flags.in_test:\n"), "if frappe.in_test:\n")
		self.assertEqual(self.fix("V16-020", "frappe.flags.in_test = True\n"), "frappe.flags.in_test = True\n")

	def test_test_case_rename_skips_mixed_imports(self):
		self.assertEqual(self.fix("V16-042", "from frappe.tests.utils import FrappeTestCase\n"), "from frappe.tests import IntegrationTestCase\n")
		mixed = "from frappe.tests.utils import FrappeTestCase, change_settings\n"
		self.assertEqual(self.fix("V16-042", mixed), mixed)
		self.assertEqual(self.fix("V16-042", "class T(FrappeTestCase):\n"), "class T(IntegrationTestCase):\n")

	def test_value_cache(self):
		src = 'frappe.db.value_cache.pop(("User", "Guest", "full_name"), None)\n'
		self.assertEqual(self.fix("V16-029", src), 'frappe.db.value_cache["User"]["Guest"].pop("full_name", None)\n')

	def test_json_sort_field(self):
		out = self.fix("V16-016", '{\n "sort_field": "modified",\n "x": 1\n}', line=0)
		self.assertEqual(json.loads(out)["sort_field"], "creation")

	def test_validation(self):
		self.assertFalse(self.fixer._valid("x.py", "a = 1\n", "a = (\n"))
		self.assertTrue(self.fixer._valid("x.json", "{}", "{}"))


class TestMisc(unittest.TestCase):
	def test_glob(self):
		try:
			from upgrade_guard.engine.scan import glob_match
		except ImportError:
			self.skipTest("frappe not available")
		self.assertTrue(glob_match("a/report/x/x.js", "**/report/**/*.js"))
		self.assertTrue(glob_match("build.json", "**/build.json"))
		self.assertFalse(glob_match("a/b.txt", "**/*.js"))

	def test_compare_snapshots(self):
		try:
			from upgrade_guard.engine.snapshot import compare
		except ImportError:
			self.skipTest("frappe not available")
		a = {"doctypes": {"X": {"all": 5}}, "schema": {"X": ["a:Data", "b:Data"]}, "totals": {"GL Entry.debit": 10.0}, "prints": {"P": {"status": "ok", "length": 1000}}, "reports": {}}
		b = {"doctypes": {"X": {"all": 4}}, "schema": {"X": ["a:Data"]}, "totals": {"GL Entry.debit": 10.0}, "prints": {"P": {"status": "error", "error": "boom"}}, "reports": {}}
		reg = "\n".join(compare(a, b)["regressions"])
		for needle in ("record counts", "fields removed", "Print Format 'P' now fails"):
			self.assertIn(needle, reg)
		self.assertEqual(compare(a, a)["regressions"], [])


if __name__ == "__main__":
	unittest.main()
