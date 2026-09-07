# Automatic skill selection

CyntOX selects relevant installed skills by default. You describe the task; no skill command is required.

```text
cyntox run
cyntox ask debug this Python function
cyntox council plan a Jellyfin media server
```

The interactive assistant selects guidance for each submitted prompt. Queued tasks and direct councils use the same local selector. It matches intent phrases and installed skill metadata, loads at most three automatic selections, and keeps the injected guidance bounded. It makes no network request or additional model call. General conversation does not need a skill, and lexical matching is intentionally conservative rather than a guarantee of perfect semantic understanding.

The starter library includes coding, PC administration, media servers, OS-lab work, research notes, and privacy/security. New repo-local skills can participate through descriptive `SKILL.md` names, descriptions, and trigger metadata; no router code change is required. Archived, malformed, unreadable, oversized, or out-of-tree files are not loaded.

## Optional controls

| Command | Behavior |
| --- | --- |
| `cyntox run` | Automatic selection during chat |
| `cyntox ask debug this function` | Automatic selection for a job |
| `cyntox use coding debug this function` | Manual job selection; no automatic additions |
| `cyntox run --use-skill coding` | Manual selection for this chat session |
| `cyntox council --use-skill coding debug this function` | Manual council selection |
| `cyntox run --no-auto-skills` | No automatic chat skills |
| `cyntox ask --no-auto-skills explain this idea` | No automatic skills for this job |
| `cyntox council --no-auto-skills explain this idea` | No automatic council skills |
| `cyntox skills` | List the installed library |

Repeat `--use-skill` to select multiple skills in an interactive session or direct council. Explicit manual selection takes precedence over automatic selection and its opt-out flag. Missing manual selections produce an error rather than pretending the requested guidance was loaded.

## Evidence and boundaries

A short notice identifies chosen skills. Job and council manifests include selection mode, names, reasons, and source hashes. Saved tasks preserve those sources through retry/resume; changed or removed guidance fails visibly. Submit a new task to select against an updated library.

Interactive selection metadata stays under the ignored `.oslab/cyntox/skill-selections` directory. These routing records contain hashes and selection details, not raw task text or reasoning. This does not change the assistant's existing conversation/session storage or the normal task artifacts saved for jobs and councils.

Skill instructions are advisory. They cannot authorize commands, change device approvals, enable networking, disclose secrets, or override higher-priority safety rules. Automatic selection does not generate or install skills. Council skill creation still requires both `--allow-skill-create` and `--mode implement`; the interactive runtime's separate automatic skill-creation feature remains disabled.

Interactive integration uses CyntOX Code's local `UserPromptSubmit` hook, supported by the upstream [Qwen Code hook protocol](https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/). The existing shell privacy hook remains active. The selection hook runs with either the loopback proxy or the direct local Ollama fallback.
