---
id: blocking_loading_gate
title: Blocking loading gate in root layout
severity: high
complexity: medium
confidence: 0.85
max_turns: 15
---

# Blocking Loading Gate in Root Layout

Detect root/layout wrapper components that block ALL children behind a full-page loading state while auth, profile, or onboarding checks run. Examples: a `<LoadingScreen />` rendered in a root-level provider/wrapper until async checks complete, preventing any child route from painting.

## Detection strategy

1. Find components mounted in root layout or top-level providers (`layout.tsx`, `root-layout`, `_app.tsx`).
2. Look for conditional rendering patterns: `if (isLoading) return <LoadingScreen />` or `if (!user) return <Loader />`.
3. Check if the blocking condition involves async operations (auth check, profile fetch, onboarding status).
4. The component must wrap multiple routes — single-page loading states are not this pattern.

## Verification

Before reporting, confirm:
- The component is in the render path of the root layout.
- Removing or deferring the gate would allow child routes to paint immediately.

## Output rules

- Set `type` to `"blocking_loading_gate"`.
- The `solution` should describe moving the check to a non-blocking pattern (Suspense boundary, client-side effect after paint, streaming).
