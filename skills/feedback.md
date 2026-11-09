---
name: feedback
description: Analyze merged autopr PRs and generate new playbook patterns from recurring fixes
user_invocable: true
---

# AutoPR Feedback — Learn Patterns from Merged PRs

You are analyzing merged autopr PRs to extract recurring patterns that can be turned into new playbook files for future runs.

## Arguments
- `--since <days>` — Only look at PRs merged in the last N days (default: all)
- `--repo <owner/repo>` — GitHub repo to analyze (default: detect from current directory)

## Step 1: Gather merged autopr PRs

Run this command to fetch all merged autopr PRs:

```bash
gh pr list --state merged --search "head:autopr/" --json number,title,body,headRefName,mergedAt --limit 100
```

If the user passed `--since`, filter by `mergedAt` date.

For each PR, also fetch the diff:

```bash
gh pr diff <number>
```

## Step 2: Read existing playbook patterns

Read all `.md` files in the `playbook/` directory (including subdirectories) to understand what patterns already exist. Each file has frontmatter with an `id` field. Collect all existing pattern IDs so you don't create duplicates.

## Step 3: Analyze and group

For each merged PR, extract:
- **What type of fix was it?** (dead code removal, query batching, DRY extraction, redundant lookup elimination, performance optimization, etc.)
- **What files/modules were touched?**
- **What was the core pattern?** (the reusable insight, not the specific fix)
- **Did the AI Review section flag any issues?** What passed, what failed?

Group PRs by pattern type. Look for:
- Patterns that appeared in 2+ PRs (strong signal for a playbook entry)
- Single-PR patterns that are clearly generalizable
- Failed/rejected patterns to understand what NOT to look for

## Step 4: Discuss findings with the user

Present your analysis as a table:

| Pattern | PRs | Existing Playbook? | Action |
|---------|-----|-------------------|--------|
| Sequential single-ID queries batched | #158, #165 | Yes: `batch_queries` | Skip — already covered |
| Duplicate code block extraction | #154, #162 | Yes: `duplicate_code` | Skip — already covered |
| Unused module deletion | #157, #163, #164 | Yes: `unused_files` | Skip — already covered |
| [NEW PATTERN] | #NNN | No | Create playbook |

Ask the user which new patterns to create playbook files for.

## Step 5: Generate playbook files

For each approved pattern, create a file in `playbook/learned/`:

```markdown
---
id: learned_<pattern_id>
title: <Pattern title>
severity: <low|medium|high>
complexity: <low|medium|high>
confidence: <0.70-0.95>
max_turns: 15
source: autofeedback
learned_from: "<PR numbers>"
---

# <Pattern Title>

<1-2 sentence description of what to look for.>

## Detection strategy

1. <Step 1 — where to look>
2. <Step 2 — what code pattern to match>
3. <Step 3 — how to confirm it's real>

## Verification

Before reporting, confirm:
- <Check 1>
- <Check 2>

## Output rules

- Set `type` to `"<pattern_id>"`.
- The `solution` should describe <what the fix looks like>.
```

Important:
- The `id` field must be unique across all playbook files
- Use `source: autofeedback` to distinguish learned patterns from hand-written ones
- Include `learned_from` with the PR numbers that inspired the pattern
- The detection strategy should be general enough to find similar issues in other codebases, not specific to the PRs that inspired it

## Step 6: Summary

After writing files, print:
- How many PRs were analyzed
- How many new patterns were created
- File paths of the new playbook files
- Reminder: these patterns will be picked up automatically by `autopr run` and `autopr scan`
