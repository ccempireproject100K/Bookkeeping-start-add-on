# SKILLS.md — skills carried by the bookkeeping crews

_Owner standing rule (2026-10-31): the screened free GitHub agent skills are
equipped to every crew AND carried in each repo the crews work in. This
manifest names what applies in THIS repo. It **names** skills — it does not
copy them. Screening record: `~/workspace/empire-bots/SKILLS_SCREENING.md`
(operator's workspace). Discipline: only ADOPT-verdict skills are ever copied
into a repo; ADAPT skills are method references only (never copy their text);
REJECT / UNVERIFIED never enter a repo._

## Adopted general skills (ADOPT verdict; Apache-2.0)

Full text + provenance for these three live in the operator's central library
(`~/workspace/custom-skills/`, with `PROVENANCE.md`): **skill-creator**,
**mcp-builder**, **changelog-generator** (Apache-2.0). They are equipped to
the crews from that library; a repo copy is made only when a crew actually
works the skill inside this repo.

| Skill | Applies where in this repo |
|---|---|
| `skill-creator` | Building/testing any new crew skill (e.g. a future niche-wiring skill) with baselines and assertions before it is trusted. |
| `mcp-builder` | Developer-side reference if bookkeeping data (ledgers, packs, DataClean) is ever exposed as MCP tools. |
| `changelog-generator` | Turning shipped commits on `main` into customer-language release notes at ship time (review before publish). |

## The empire's own learned skills (in-house; central library)

| Skill | Applies where in this repo |
|---|---|
| `bookkeeping-bakeoff-buildout` | The parity/bake-off build discipline this codebase was built under: spec-first slices, baseline test deltas, no fake zeros. |
| `github_target_verification` | Every push: exact repo/branch confirmed, landing verified from commit history (`list_commits`), never from the read snapshot. |
| `post_merge_deploy_verification` | Every ship: a merge is not a deploy — verify the live API + web services serve the new head before reporting shipped. |
| `scope_stage_decision_readback` | Build → wiring → deploy are separate stages, each with its own go-ahead; a finished build is never read as a deploy approval. |
| `coverage_census_before_complete` | Before any surface is reported complete: enumerate the universe independently and recount — e.g. the niche census (36 packs × subdomains × icons) behind the subdomain wiring. |
| `lost_report_recovery_from_disk` | When a crew's completion report is cut off: recover the work from the staging tree/disk first; respawn only what is actually missing. |

## DataClean build skills (ADOPT verdict; MIT)

Equipped for the DataClean power-up build (owner directive 2026-11-03).
Full text + provenance live in the operator's central library
(`~/workspace/custom-skills/`, each with its upstream license file);
mapping record: `~/workspace/buildout/security-skills-harvest/SHORTLIST.md`
§(b) (operator's workspace). These are named here, not copied.

| Skill | Applies where in this repo |
|---|---|
| `data-cleaning-pipeline` | `backend/lib/dataclean/` — staged normalize → validate → dedupe pipeline discipline; engine stays framework-free and lifts wholesale. |
| `data-qa` | DataClean test gates: labeled-fixture precision == 1.00 ship gate, verbatim-post regression, per-phase test evidence before any phase is called done. |
| `crm-data-cleanup` | Merge law for every surface incl. the Studio queue: never auto-merge on shared/role identifiers, never overwrite a non-empty value with an empty one, flag-don't-delete, the human executes merges — snapshot-backed and reversible. |
| `csv-excel-merger` | Bulk import + cross-list work: importer mapping discipline, cross-list dedupe semantics (pairs only across the A × B boundary), batch job chunking. |

## ADAPT methods — used by reference only (never copied)

| Method (source) | Applies where in this repo |
|---|---|
| Trade × market skin resolver pattern (in-house, `backend/lib/market_packs.py`) | Composing a niche pack with a market face; subdomain host resolution reuses the same pack universe — one taxonomy for Momo, bookkeeping, and Compliance Finder. |
