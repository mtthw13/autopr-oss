---
id: orphaned_asset
title: Orphaned non-code assets
severity: low
complexity: medium
confidence: 0.85
max_turns: 15
---

# Orphaned Non-Code Assets

Find template files, prompt files, config fragments, and other non-code assets that are no longer reachable from application code.

Unlike unused Python/TypeScript modules (which can be caught by import analysis), these assets are referenced through string literals, template inclusion chains, or configuration lookups — so simple grep-for-import misses them.

## Detection strategy

1. Identify asset directories: Jinja/Handlebars templates, prompt files, SQL fragments, email templates, i18n bundles, etc.
2. Collect **root references** from application code — string literals or config entries that name an asset file (e.g., `"system_prompt.jinja"`, `"welcome_email.html"`).
3. Walk **transitive references** from those roots: template directives like `{% include %}`, `{% extends %}`, `{% import %}`, or equivalent in Handlebars/EJS/Liquid.
4. Any asset file not in the live set (roots + transitively reachable) is orphaned.

## Verification

Before reporting, confirm:
- The asset is not referenced by a dynamic/computed string (e.g., `f"prompt_{variant}.jinja"`). Search for partial name matches and glob patterns.
- The asset is not loaded via a config file, environment variable, or feature flag that wouldn't appear as a literal in code.
- The asset is not used exclusively in tests — check test directories separately.

## Output rules

- Each orphaned asset file is ONE opportunity.
- Set `type` to `"orphaned_asset"`.
- The `solution` should be: "Delete the orphaned asset after confirming it is not referenced dynamically or via configuration."
