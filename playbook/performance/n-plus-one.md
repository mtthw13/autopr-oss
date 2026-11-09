---
id: n_plus_one
title: N+1 queries
severity: high
complexity: low
confidence: 0.90
max_turns: 15
---

# N+1 Queries

Find loops that make individual database or API calls per item when they could be batched into a single call.

## Detection strategy

1. Look for `for` / `async for` / list-comprehension loops in service layers, commands, and tool files.
2. Inside each loop body, check for calls to functions that hit the database or an external API — common patterns:
   - `get_*_by_id(single_id)` called inside a loop
   - `await client.fetch(...)` per iteration
   - ORM `.get()` or `.filter(id=...)` per iteration
3. Check whether a batch/bulk equivalent exists (e.g., `get_*_by_ids`, `filter(id__in=...)`, `WHERE id = ANY(...)`).

## Verification

Before reporting, confirm:
- The batched alternative exists or is trivially creatable from the existing API.
- The loop processes more than one item in realistic usage.
- Batching won't change the return shape or error handling behavior.

## Output rules

- Each distinct loop location is ONE opportunity.
- Set `type` to `"n_plus_one"`.
- Include the loop's file and line range in `line_ranges`.
- The `solution` should describe collecting IDs → single batched call → dict lookup to distribute results.
