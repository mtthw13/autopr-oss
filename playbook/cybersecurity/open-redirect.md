---
id: open_redirect
title: Open redirect via URL validation bypass
severity: high
complexity: low
confidence: 0.85
max_turns: 15
---

# Open Redirect via URL Validation Bypass

Find redirect/navigation flows where URL validation can be bypassed to redirect users to attacker-controlled domains.

## Detection strategy

1. Search for redirect parameters (`redirect`, `returnTo`, `next`, `callback`, `url`, `continue`, `dest`) in route handlers, middleware, and auth flows.
2. For each redirect target, check the validation logic for these bypass-prone patterns:
   - `startsWith("/")` or `charAt(0) === "/"` — accepts protocol-relative URLs like `//evil.com`.
   - Regex that allows `//` after the leading slash (e.g., `/^\//` without rejecting a second `/`).
   - `new URL(userInput, base)` where an absolute or protocol-relative `userInput` overrides the base entirely.
   - Host allowlist using `includes()` or `endsWith()` on the hostname — matches `evilexample.com` for an `example.com` check.
3. Check for missing validation entirely — redirect targets taken from query params or POST bodies and passed directly to `res.redirect()`, `Location` header, `window.location`, or `router.push()`.
4. Look for double-encoding or backslash tricks: `\/evil.com` or `/%2Fevil.com` that survive decoding into `//evil.com`.

## Verification

Before reporting, confirm:
- The redirect target is attacker-controllable (comes from user input, query string, or request body — not a hardcoded value or server-only config).
- The redirect actually executes in a browser context (server-side API-to-API redirects with no browser involvement are not exploitable for phishing).
- The bypass works end-to-end: craft a concrete payload (e.g., `//evil.com`) and trace it through the validation to confirm it passes.

## Output rules

- Each distinct redirect endpoint is ONE opportunity.
- Set `type` to `"open_redirect"`.
- Include the validation code's file and line range in `line_ranges`.
- The `solution` should describe rejecting protocol-relative URLs (e.g., strip or reject paths matching `/^\/\//` or use `new URL()` and assert the origin matches the app's own origin).
