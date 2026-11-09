---
id: unused_file
title: Unused files
severity: low
complexity: low
confidence: 0.95
max_turns: 15
---

# Unused Files

Find entire Python/TypeScript modules that are never imported or referenced anywhere in the codebase.

## Detection strategy

1. List all `.py` and `.ts`/`.tsx` files under the scan paths.
2. For each file, search the rest of the codebase for any import statement or dynamic reference to that module.
3. Ignore `__init__.py`, `conftest.py`, migration files, and entry points (e.g., `main.py`, `cli.py`).
4. A file is unused if NO other file imports it and it is not an entry point, test config, or framework-required file.

## Verification

Before reporting, confirm:
- `grep -r` for the module name (without extension) returns zero results outside the file itself.
- The file is not referenced in any config file (pyproject.toml, package.json, Makefile, Dockerfile, etc.).
- The file is not a CLI entry point, migration, fixture, or test configuration.

## Output rules

- Each unused file is ONE opportunity.
- Set `type` to `"unused_file"`.
- The `solution` should be: "Delete the file after verifying it has no runtime references."
