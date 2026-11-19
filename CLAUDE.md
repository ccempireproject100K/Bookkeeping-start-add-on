# CLAUDE.md

<!-- Token-efficiency rules from github.com/drona23/claude-token-efficient (MIT). User instructions override these. -->

## Approach
- Read existing files before writing. Don't re-read unless changed.
- Thorough in reasoning, concise in output.
- Skip files over 100KB unless required.
- No sycophantic openers or closing fluff.
- No emojis or em-dashes.
- Do not guess APIs, versions, flags, commit SHAs, or package names. Verify by reading code or docs before asserting.

## Compact instructions
When compacting, keep: current task and plan, files changed, failing test output and errors, decisions made. Drop: full file dumps and passing test output.
