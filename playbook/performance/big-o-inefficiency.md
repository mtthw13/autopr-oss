---
id: big_o_inefficiency
title: Algorithmically inefficient code paths
severity: medium
complexity: medium
confidence: 0.80
max_turns: 15
---

# Algorithmically Inefficient Code Paths

Identify code where a lower Big-O complexity is achievable with a straightforward refactor.

## Detection strategy

1. Search for nested loops that iterate over the same or related collections — these are often O(n²) when an O(n) hash-based approach exists.
2. Look for linear searches (`.find`, `.index`, `in list`, manual iteration) inside loops — replaceable with a dict/set lookup.
3. Flag repeated sorting of the same collection across a call chain.
4. Identify repeated computation of the same value inside loops (memoization candidate).
5. Look for string concatenation in loops instead of join/builder patterns.

## Verification

Before reporting, confirm:
- The inner data structure actually grows with input (not a small fixed-size list).
- The hot path is reachable in production, not just test helpers or one-time setup.
- The proposed alternative doesn't sacrifice correctness (e.g., hash-based approach requires hashable keys).
- The collection isn't already bounded by a constant (e.g., iterating over enum values is fine).

## Output rules

- Each distinct location is ONE opportunity.
- Set `type` to `"big_o_inefficiency"`.
- In `description`, state the current complexity and the achievable complexity (e.g., "O(n²) → O(n) via set lookup").
- In `solution`, describe the specific refactor.
- Group related inefficiencies in the same function as one opportunity.
