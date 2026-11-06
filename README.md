# Upgrade Guard

Know exactly what will break **before** you upgrade Frappe / ERPNext — and fix most of it automatically.

Upgrade Guard reads version-hop rule packs (derived from Frappe's *Migrating to version N* wiki pages),
scans every custom app and every site customization, and tells you **GO / GO WITH FIXES / NO-GO**,
with a ranked list of what to fix, where, and how.

## Features
| | |
|---|---|
| **Any upgrade path** | 12→13, 13→14, 14→15, 15→16 packs; any chain (13→16 loads three). Add `vN_to_vN+1.yaml` for the future. |
| **Source scan** | Python (AST, import-alias aware), JS/Vue, Jinja/HTML, JSON (DocTypes, workspaces, print formats), `hooks.py`, `pyproject.toml` |
| **Hook analysis** | follows `doc_events`, `has_permission`, `override_doctype` into the real functions |
| **Site customizations** | Server Script, Client Script, Custom HTML Block, Print Format, Report, Web Form, Web Page, Notification, Email Template, Workflow, Assignment Rule, Webhook, Dashboard Chart, Number Card, Property Setter |
| **Environment** | Python / Node / Chromium requirements, official-app branches, apps capping `frappe<N` |
| **Verdict + score + effort** | blockers → NO-GO; low-confidence rules are down-weighted |
| **Auto-fixer** | dry-run diff, apply with backup, one-command undo, parse-validation, git-dirty guard |
| **Regression snapshots** | counts, schema, totals, custom print renders and reports before vs after |
| **Runbook** | tailored backup → switch → migrate → verify → rollback steps |
| **Extensible** | other apps can ship rule packs via the `upgrade_guard_rules` hook |

## Install
```bash
bench get-app <repo-url> && bench --site <site> install-app upgrade_guard
```
Run it on a **staging copy** of your site. Scanning is read-only.

## Use
```bash
bench --site X upgrade-check   --target 16                 # source auto-detected; --source 13 for older
bench --site X upgrade-check   --target 16 --format html --out report.html
bench --site X upgrade-fix     --target 16                 # dry run: prints a diff
bench --site X upgrade-fix     --target 16 --apply         # writes, with backup in ./upgrade_guard_backups
bench --site X upgrade-fix-undo upgrade_guard_backups/<timestamp>
bench --site X upgrade-snapshot --out before.json --reports "General Ledger::{\"company\":\"ACME\"}"
bench --site X upgrade-compare before.json after.json      # exit 1 on regressions
bench --site X upgrade-runbook --target 16 --out RUNBOOK.md
```
`upgrade-check` exits 0 / 1 / 2 for GO / GO WITH FIXES / NO-GO, so it can gate CI.
The desk UI is **Upgrade Readiness Check** (Run scan, findings table, Fix → Preview auto-fixes).

## Writing rules
Rules are data (`upgrade_guard/rules/vA_to_vB.yaml`):
```yaml
- id: V16-020
  title: "frappe.flags.in_test deprecated"
  severity: medium            # blocker | high | medium | low | info
  confidence: high            # low = down-weighted in verdict
  kind: py_name               # see tests/test_engine.py VALID_KINDS
  names: ["frappe.flags.in_test"]
  fix: "Use frappe.in_test"
  link: https://github.com/frappe/frappe/pull/32960
  autofix: {replace: [{pattern: "frappe\\.flags\\.in_test", with: "frappe.in_test", unless: "in_test\\s*=(?!=)"}]}
```
Run `python -m unittest discover -s upgrade_guard/tests -t .` — it lints every rule.

## Limits (read this)
- Static analysis finds *known, listed* breaking changes. It cannot prove a site will work. Always rehearse on a restored copy and compare snapshots.
- Rules marked `confidence: low` are heuristics and will produce false positives.
- Developed and tested against Frappe 15 sites; the 12→13 and 13→14 packs are short because the upstream guides are short.
- The fixer edits files; run it on a git branch. Applying from the UI requires `developer_mode`.

MIT licensed.
