---
id: first_paint_client_fetch
title: Above-the-fold data fetched client-side after mount
severity: high
complexity: medium
confidence: 0.85
max_turns: 15
---

# Above-the-Fold Data Fetched Client-Side After Mount

Detect pages where above-the-fold content is fetched client-side via useEffect/fetch after mount, showing skeletons or spinners while data loads, when the data could be fetched server-side or prefetched.

## Detection strategy

1. Look for page components that render skeleton/placeholder states initially.
2. Find `useEffect` or `useSWR`/`useQuery` hooks that fetch data needed for the initial visible content.
3. Check if the data source is available at request time (DB query, API call with server-side credentials).
4. Focus on pages where the skeleton is the first thing users see (not lazy-loaded below-fold content).

## Verification

Before reporting, confirm:
- The fetched data is used for above-the-fold rendering.
- The data could be fetched server-side (Next.js server component, `getServerSideProps`, or RSC data fetching).

## Output rules

- Set `type` to `"first_paint_client_fetch"`.
- The `solution` should describe moving the fetch to server-side rendering or prefetching.
