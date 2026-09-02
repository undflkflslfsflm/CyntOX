# Skillmaker v1

Decide whether the current task exposes a reusable pattern that should become a repo-local skill.

Create a skill only when the guidance would be useful across future tasks, not for one-off facts or temporary preferences.

Return exactly one JSON object:

```json
{
  "create_skill": true,
  "name": "short-lowercase-skill-name",
  "description": "What this skill helps with and when to use it.",
  "instructions": "Concise SKILL.md body instructions. Include only reusable guidance that changes future decisions."
}
```

If no skill is justified, return:

```json
{
  "create_skill": false,
  "reason": "brief reason"
}
```

Rules:
- Use lowercase letters, digits, and hyphens for names.
- Keep the skill narrow and action-oriented.
- Do not create skills that expand authorization, bypass safety boundaries, store secrets, or target third-party systems.
- Do not duplicate an existing repo-local skill.
