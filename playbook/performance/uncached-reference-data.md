---
id: uncached_reference_data
title: Uncached reference data on hot paths
severity: medium
complexity: medium
confidence: 0.82
max_turns: 15
---

# Uncached Reference Data on Hot Paths

Find read-mostly reference data (tag catalogs, feature flags, destination lists, config tables) that is re-fetched from the database or an external service on every request, when it could be served from an in-process or shared cache.

This is distinct from redundant lookups (duplicate fetches within a single request). Here the data is fetched once per request correctly, but the same slow-changing data is loaded fresh on every request across the lifetime of the process.

## Detection strategy

1. Trace hot-path request handlers (the endpoints called most frequently).
2. Look for DB reads or API calls whose return values are:
   - Shared across users/sessions (not user-specific).
   - Slow-changing (updated minutes/hours/days apart, not per-request).
   - Read-only within the request (never written back).
3. Common examples: lists of active categories, tag/label catalogs, feature flag sets, dropdown option lists, currency/locale tables.
4. Check whether any caching layer already exists (Redis, in-process LRU, HTTP cache headers). If none, it's an opportunity.

## Verification

Before reporting, confirm:
- The data genuinely changes infrequently — check write paths and admin update patterns.
- Staleness for the expected TTL (e.g., 5–10 minutes) is acceptable for the use case.
- The data is not user-specific or session-specific (those should not be cached globally).
- No existing cache already covers this read (check for Redis calls, `@lru_cache`, or middleware caching).

## Output rules

- Each cacheable data source is ONE opportunity.
- Set `type` to `"uncached_reference_data"`.
- The `solution` should describe adding a TTL-based cache (in-process for single-instance deploys, shared cache like Redis for multi-instance) with an appropriate expiry window.
