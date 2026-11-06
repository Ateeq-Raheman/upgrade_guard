"""Loads version-hop rule packs.  Each hop (N -> N+1) lives in its own YAML file, so any
upgrade path (13->16, 14->15 ...) is simply the chain of hops between source and target.
Third-party apps can ship extra packs via the `upgrade_guard_rules` hook (list of dirs)."""
import os

import yaml

RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")


def _dirs():
	dirs = [RULES_DIR]
	try:
		import frappe

		for path in frappe.get_hooks("upgrade_guard_rules") or []:
			if os.path.isdir(path):
				dirs.append(path)
	except Exception:
		pass
	return dirs


def _load_file(path):
	with open(path, encoding="utf-8") as f:
		return yaml.safe_load(f) or {}


def available_hops():
	hops = set()
	for d in _dirs():
		for fn in os.listdir(d):
			if fn.startswith("v") and fn.endswith(".yaml") and "_to_v" in fn:
				a, b = fn[1:-5].split("_to_v")
				hops.add((int(a), int(b)))
	return sorted(hops)


def load_rules(source, target):
	"""Return (rules, warnings) for the chain source -> target."""
	rules, warnings, seen = [], [], set()
	if target <= source:
		raise ValueError("Target version must be higher than the source version")
	for a in range(source, target):
		b = a + 1
		found = False
		for d in _dirs():
			path = os.path.join(d, f"v{a}_to_v{b}.yaml")
			if not os.path.exists(path):
				continue
			found = True
			pack = _load_file(path)
			for r in pack.get("rules", []):
				if r["id"] in seen:
					continue
				seen.add(r["id"])
				r["hop"] = f"{a}→{b}"
				r["hop_from"], r["hop_to"] = a, b
				r.setdefault("confidence", "high")
				r.setdefault("severity", "medium")
				r.setdefault("category", "other")
				r.setdefault("fix", "")
				r.setdefault("languages", ["python", "js", "html"])
				rules.append(r)
		if not found:
			warnings.append(f"No rule pack for v{a} → v{b}; this hop was NOT checked.")
	return rules, warnings
