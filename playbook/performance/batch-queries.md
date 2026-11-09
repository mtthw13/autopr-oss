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
   - Example: calling `get_entities_by_ids([id1])` then `get_entities_by_ids([id2])` instead of `get_entities_by_ids([id1, id2])`.
2. Look for the same fetch function called from multiple branches of a conditional or multiple slots of a data structure.
3. Check whether the underlying function already supports batch input (list of IDs, `WHERE IN`, etc.).

## Verification

Before reporting, confirm:
- The calls are to the same function or endpoint.
- The function already accepts multiple IDs or items (or is trivially extendable).
- Combining won't change error semantics (e.g., partial failures handled the same way).

## Output rules

- Group related sequential calls into ONE opportunity per function/location.
- Set `type` to `"batch_queries"`.
- The `solution` should describe collecting all arguments → one call → distributing results.
