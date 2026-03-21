# openai.yaml Reference

Use this reference when creating `agents/openai.yaml` for an installable skill.

## Required Shape

```yaml
interface:
  display_name: "Human-facing skill name"
  short_description: "One-line description of what the skill does and when to use it"
  default_prompt: |
    Short default operating prompt for the skill.
```

## Field Rules

- `display_name`
  - Use a human-facing title.
  - Keep it short and readable in UI lists.
- `short_description`
  - Describe the skill outcome and the main trigger/use case.
  - Keep it to one sentence when possible.
- `default_prompt`
  - Summarize the operating model for the skill.
  - Keep it concise and deterministic.
  - Do not restate the full `SKILL.md`.

## Good Defaults

- Prefer stable wording over marketing language.
- Reflect the actual `SKILL.md` contents.
- Regenerate metadata when the trigger surface or core workflow changes.

## Multi-Skill Repos

- Each installable skill should have its own `agents/openai.yaml`.
- Do not reuse one skill's metadata across different subskills without reviewing the trigger wording.
