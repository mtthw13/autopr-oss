---
id: commented_code
title: Commented-out code
severity: low
complexity: low
confidence: 0.95
max_turns: 15
---

# Commented-Out Code

Find large blocks of commented-out code that should be removed since the codebase is under version control.

## Detection strategy

1. Scan source files for consecutive commented lines (3+ lines) that look like they were once executable code — not documentation comments or TODOs.
2. Indicators of commented-out code vs. documentation:
   - Contains function calls, variable assignments, imports, or control flow.
   - Is inside a function body (not at module level as a docstring).
   - Uses language syntax (e.g., `# if foo:`, `// const x = ...`, `# return result`).
3. Ignore: license headers, docstrings, TODO/FIXME annotations, and configuration examples.

## Verification

Before reporting, confirm:
- The block is genuinely dead code, not an intentional usage example or documentation.
- The block is at least 5 lines long (small one-liners may be intentional notes).

## Output rules

- Each file with significant commented-out code is ONE opportunity.
- Set `type` to `"commented_code"`.
- The `solution` should be: "Remove the commented-out code block. It's preserved in git history if ever needed."
