---
id: serial_hot_path_io
title: Sequential independent I/O on hot paths
severity: high
complexity: low
confidence: 0.90
max_turns: 15
---

# Sequential Independent I/O on Hot Paths

Detect independent DB queries, API calls, or auth checks that are awaited sequentially on request startup or page render paths when they could run in parallel.

## Detection strategy

1. Look for multiple sequential `await` calls on request handlers, server components, or page loaders.
2. Check if the calls are truly independent — no data dependency between them.
3. Focus on hot paths: page renders, API route handlers, middleware chains.
4. Common pattern: `const user = await getUser(); const prefs = await getPrefs(); const data = await getData();` when none depend on each other.

## Verification

Before reporting, confirm:
- The calls are independent (output of one is not input to another).
- Parallelizing them with `Promise.all` or `asyncio.gather` won't change semantics.

## Output rules

- Set `type` to `"serial_hot_path_io"`.
- The `solution` should describe using `Promise.all` (JS) or `asyncio.gather` (Python) to parallelize the independent calls.
