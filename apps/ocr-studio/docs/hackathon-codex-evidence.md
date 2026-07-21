# PredixaLearn: Codex development evidence

This record separates verified evidence from information that only the project
owner can supply. Do not replace a pending item with a claim that cannot be
shown to judges.

## Verified readiness session — 2026-07-21

- Scope: hackathon-readiness verification for the PredixaLearn repository.
- Codex contribution: installed the pinned frontend dependencies from the lockfile,
  ran the TypeScript and Playwright gates, and exercised the live local Judge
  Demo through OCR, local analysis, and Markdown/JSON/DOCX downloads.
- Verified result: TypeScript passed; 13 Playwright tests passed; the live,
  two-page synthetic Judge Demo completed with local source-linked analysis and
  all three analysis exports.
- Human review: the presenter must review the recorded terminal output and attach
  the Codex session/feedback identifier before submission.

This verification session demonstrates responsible release validation. It does
not by itself prove who designed or implemented historical product features.

## Required owner-supplied evidence before submission

| Evidence | What to attach | Status |
| --- | --- | --- |
| Codex session or feedback identifier | Shareable session/feedback link or identifier | Pending owner input |
| Repository release | Immutable commit SHA or release tag used for the demo | Pending release selection |
| Presenter and reviewer | Names and review date | Pending owner input |
| Screenshots or clips | Source page, result, warning, analysis, and teacher-review screens | Capture using `docs/submission-assets/README.md` |

## Feature evidence records

Complete each row only with a reviewable commit, test, fixture, or session.

| Area | Problem and Codex contribution | Human decision | Verifiable evidence | Status |
| --- | --- | --- | --- | --- |
| Source-linked question extraction | Describe the observed numbering or boundary issue and the Codex-assisted change. | Describe the selected or corrected behavior. | Commit, tests, and fixture paths. | Pending owner input |
| Table or formula uncertainty | Describe the unsafe reconstruction case and Codex-assisted investigation. | State the threshold or rejected suggestion. | Regression test and sample output. | Pending owner input |
| Past Paper Intelligence grounding | Describe the unsupported-source-reference or AI-safety problem. | State the approved source-link and review behavior. | Contract test and reviewed analysis. | Pending owner input |

## Submission rules

- State that deterministic OCR reconstruction precedes optional, source-linked
  GPT analysis.
- Show evidence, warnings, and teacher review beside at least one analysis item.
- Report only permission-cleared benchmark results with source, hardware, model,
  and limitation details.
- Say that PredixaLearn supports historical revision prioritization; it does not
  predict exact future examination questions.
