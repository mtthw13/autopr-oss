---
id: duplicate_code
title: Duplicate code
severity: medium
complexity: medium
confidence: 0.85
max_turns: 15
---

# Duplicate Code

Find near-identical logic repeated in multiple places that could be extracted into a shared helper.

## Detection strategy

1. Look for functions or code blocks across different files that do essentially the same thing with minor variations (different variable names, slightly different parameters).
2. Common duplication sites:
   - API route handlers that repeat the same validation/transformation logic.
   - Multiple service functions that build the same query structure.
   - Copy-pasted error handling blocks.
3. Focus on duplications where the shared logic is 10+ lines — trivial one-liners are not worth extracting.

## Verification

Before reporting, confirm:
- The duplicated blocks are genuinely doing the same thing (not just superficially similar).
- Extracting a shared helper won't create awkward coupling between unrelated modules.
- The fix is a straightforward extraction — not a major architectural change.

## Output rules

- Each set of duplicated blocks is ONE opportunity.
- Set `type` to `"duplicate_code"`.
- List ALL files that contain the duplicated code.
- The `solution` should describe extracting the shared logic into a helper and calling it from each site.
