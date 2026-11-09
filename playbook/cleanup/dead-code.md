---
id: dead_code
title: Dead code
severity: low
complexity: low
confidence: 0.93
max_turns: 5
---

# Dead Code

Find functions, methods, classes, or constants that are defined but never called or referenced anywhere in the codebase.

## Detection strategy

1. Scan for function/method/class definitions in the source files.
2. For each definition, search the entire codebase for references to that name (excluding the definition itself and its own file's internal usage if it's a private helper that's also unused).
3. Pay attention to:
   - Private methods (prefixed `_`) that are never called within their own class.
   - Utility functions that were used by deleted code.
   - Constants defined but never referenced.
4. Ignore: test files, `__init__` methods, abstract methods, interface implementations, and framework hooks (e.g., FastAPI dependency functions, signal handlers).

## Verification

Before reporting, confirm:
- `grep -r "function_name"` across the repo returns only the definition (and maybe a comment).
- The function is not registered dynamically (e.g., via decorators like `@app.route`, `@celery.task`, plugin registries).
- The function is not part of a public API or SDK that external consumers use.

## Output rules

- Group related dead code in the same file into ONE opportunity (e.g., 3 unused functions in the same utils file = 1 opportunity).
- Set `type` to `"dead_code"`.
- The `solution` should be: "Remove the dead code after confirming zero references."
