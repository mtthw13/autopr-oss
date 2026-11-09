# Manifest Format Reference

A manifest is a JSON file containing an array of opportunity objects. Use manifests when you already know what to fix and want to skip the scanning step.

## Schema

```json
[
  {
    "id": "batch-order-queries",
    "title": "Batch order lookups in invoice generation",
    "type": "batch_queries",
    "severity": "high",
    "files": [
      "src/orders/invoice_service.py"
    ],
    "line_ranges": {
      "src/orders/invoice_service.py": "42-70"
    },
    "problem": "get_orders_by_ids() is called once per line item when generating an invoice, producing N separate DB queries.",
    "solution": "Collect all order IDs into a single list, make one batched call, distribute results via dict lookup.",
    "estimated_impact": "Reduces N DB round-trips per invoice to 1.",
    "complexity": "low",
    "autonomous_confidence": 0.92
  }
]
```

## Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique kebab-case slug. Used as the git branch suffix (`autopr/<id>`). Must be unique across the manifest. |
| `title` | string | yes | Short PR title in imperative mood, under 72 characters. |
| `type` | string | yes | Pattern type — matches a playbook pattern ID or an `OpportunityType` value (e.g. `batch_queries`, `n_plus_one`, `unused_file`, `dead_code`, `redundant_lookup`, `duplicate_code`, `commented_code`). |
| `severity` | string | yes | One of: `trivial`, `low`, `medium`, `high`. |
| `files` | string[] | yes | Relative paths (from repo root) of files to modify. |
| `line_ranges` | object | no | Map of `file_path` to `"startLine-endLine"` hint. Helps Claude focus on the right area. Default: `{}`. |
| `problem` | string | yes | 1–3 sentences describing what's wrong. Becomes the "Problem" section of the PR body. |
| `solution` | string | yes | 1–3 sentences describing the fix. Becomes the "Solution" section of the PR body. |
| `estimated_impact` | string | no | 1 sentence on performance or quality impact. Default: `""`. |
| `complexity` | string | no | One of: `low`, `medium`, `high`. Default: `"low"`. |
| `autonomous_confidence` | float | no | 0.0–1.0. How confident you are this can be fixed autonomously. Opportunities below `min_confidence` (default 0.8) are skipped. Default: `0.9`. |

## Usage

```bash
# Generate a manifest by scanning
autopr scan /path/to/repo -o opportunities.json

# Edit the manifest — remove or modify entries
# Then run with it
autopr run /path/to/repo --manifest opportunities.json
```

## Tips

- **Review before running.** Always inspect a scanned manifest before feeding it to `autopr run`. Remove entries that look risky or wrong.
- **One opportunity per concern.** Each entry should be a single, focused fix that makes sense as one PR.
- **`files` should be complete.** List every file that needs to change. Claude will read and modify these files during the fix step.
- **`line_ranges` are hints, not constraints.** Claude will read surrounding context regardless. They help it find the right area faster.
- **Lower `autonomous_confidence` for uncertain fixes.** If you're not sure a fix is safe, set confidence to 0.7 and raise `min_confidence` in config to filter it out until you're ready.
- **A single object is also valid.** The manifest can be `{...}` instead of `[{...}]` — autopr wraps single objects into a list automatically.
