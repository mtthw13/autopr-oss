---
id: redundant_lookup
title: Redundant lookups
severity: medium
complexity: low
confidence: 0.88
max_turns: 15
---

# Redundant Lookups

Find places where the same data is fetched multiple times within a single request path when it's already available from a prior call.

## Detection strategy

1. Trace request handlers and their called functions.
2. Look for cases where a function fetches data (e.g., a full DB record) and then a downstream function fetches the same record or a subset of it again.
3. Common pattern: Function A calls `get_thing(id)` → passes the result to Function B → Function B calls `get_thing(id)` again (or queries a field that was already in A's result).

## Verification

Before reporting, confirm:
- The data from the first fetch is genuinely available (in scope, in the return value, or easily passable).
- The second fetch returns the same data (not a fresher version needed for consistency).
- The fix is a simple parameter addition or variable reuse — not a major refactor.

## Output rules

- Each redundant fetch pair is ONE opportunity.
- Set `type` to `"redundant_lookup"`.
- List both files if the redundancy spans multiple files.
