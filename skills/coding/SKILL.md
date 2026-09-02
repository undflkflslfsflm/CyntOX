---
name: coding
description: Improve or review local code changes with evidence-first engineering discipline.
---

# Coding

Use this skill when CyntOX is asked to write, modify, review, or debug code in the local workspace.

Core behavior:

- Inspect the existing code path before editing.
- Prefer the smallest change that solves the actual problem.
- Preserve unrelated user changes.
- Run targeted tests or static checks proportional to the change.
- If a test cannot run, state the exact blocker and what command should be run later.
- Keep the final answer focused on changed files, verification, and remaining risks.

Do not invent repo conventions. Derive conventions from nearby files, tests, and existing scripts.
