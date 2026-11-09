# Playbook Authoring Guide

A playbook is a directory of `.md` files — one per detection pattern. Each pattern tells autopr what kind of code issue to scan for, and autopr runs each pattern as a focused Claude Code session against the target repo.

## Pattern file format

Each `.md` file has YAML frontmatter followed by a markdown body:

```markdown
---
id: batch_queries
title: Batchable sequential queries
severity: high
complexity: low
confidence: 0.92
max_turns: 15
---

# Batchable Sequential Queries

Find multiple sequential calls that fetch similar data and could be combined into one call.

## Detection strategy

1. Look for functions that make multiple back-to-back calls to the same DB/API function with different arguments.
2. Check whether the underlying function already supports batch input.

## Verification

Before reporting, confirm:
- The calls are to the same function or endpoint.
- The function already accepts multiple IDs or items.
- Combining won't change error semantics.

## Output rules

- Group related sequential calls into ONE opportunity per function/location.
- Set `type` to `"batch_queries"`.
- The `solution` should describe collecting all arguments → one call → distributing results.
```

## Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `id` | yes | — | Unique pattern ID (snake_case). Used in `--patterns` filter and as the opportunity `type`. |
| `title` | yes | — | Human-readable pattern name, shown in CLI output. |
| `severity` | no | `low` | Default severity for opportunities found (`trivial`, `low`, `medium`, `high`). |
| `complexity` | no | `low` | Default complexity for opportunities found (`low`, `medium`, `high`). |
| `confidence` | no | `0.9` | Default `autonomous_confidence` for opportunities found (0.0–1.0). |
| `max_turns` | no | `5` | Max Claude Code tool-call turns for the scanner. More turns = deeper search but higher cost. |

## Writing the body

The markdown body becomes the scanner prompt. It should include:

### Detection strategy

Tell Claude **what to look for** and **how to find it**. Be specific about code patterns — the scanner searches by running grep, glob, and reading files.

Good:
> Look for functions that call `get_entities_by_ids([single_id])` multiple times in sequence.

Vague:
> Look for performance issues.

### Verification

Tell Claude **how to confirm** a candidate is real before reporting it. This prevents false positives.

Good:
> Before reporting, confirm the function already accepts a list of IDs (check the function signature).

### Output rules

Constrain how opportunities should be grouped and what fields should contain. The scanner outputs JSON opportunity objects with these fields: `id`, `title`, `type`, `severity`, `files`, `line_ranges`, `problem`, `solution`, `estimated_impact`, `complexity`, `autonomous_confidence`.

## Tips

- **Be specific over broad.** A pattern that finds 3 real issues is better than one that finds 20 false positives.
- **Set `max_turns` appropriately.** Simple patterns (unused files) need 3–5 turns. Complex patterns (N+1 queries) need 10–15.
- **Include examples** in the body when the pattern is subtle. Claude follows examples well.
- **Use `confidence` conservatively.** If the pattern tends to have edge cases, lower the default confidence so `min_confidence` filtering can catch uncertain results.

## Directory structure

```
my-playbook/
├── batch-queries.md
├── n-plus-one.md
├── unused-files.md
└── nah/           # subdirectories are ignored — use for drafts
    └── draft.md
```

Only `.md` files in the top level of the playbook directory are loaded. Subdirectories are ignored.

## Using your playbook

```bash
# Use with scan
autopr scan /path/to/repo --playbook /path/to/my-playbook/

# Use with run
autopr run /path/to/repo --playbook /path/to/my-playbook/

# Run only specific patterns from your playbook
autopr run /path/to/repo --playbook /path/to/my-playbook/ --patterns batch_queries,n_plus_one
```

## Built-in patterns

autopr ships with patterns in `playbook/`. You can use them as-is, copy and modify them, or write your own from scratch. See the `playbook/` directory in the repo for examples.
