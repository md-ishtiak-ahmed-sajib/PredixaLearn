# PredixaLearn hackathon submission kit

## Positioning

**Category:** Education  
**Tagline:** From difficult scans to trustworthy educational data.

PredixaLearn is a local-first document-intelligence application for students,
teachers, and educational organizations. It reconstructs difficult scanned exam
papers into structured, reviewable Markdown, DOCX, JSON, verified table data,
and figure assets while preserving source pages, confidence, geometry, and
warnings.

Past Paper Intelligence turns the deterministic reconstruction into
teacher-reviewable, source-linked questions, marks, topics, difficulty, and
revision priorities. Optional GPT analysis is bounded, consented, and rejected
when it refers to unknown source evidence. PredixaLearn analyzes historical
patterns; it does not predict exact future examinations.

## Verified product evidence

The following checks ran in this workspace on 2026-07-21. They validate product
behavior; they are not real-world OCR-accuracy claims.

- Python suite: 192 passed; 1 opt-in GPU stress test skipped.
- Python lint: Ruff passed.
- Frontend: TypeScript passed; 13 Playwright tests passed.
- Dependency audit: `npm ci` completed with no known npm vulnerabilities.
- Live Judge Demo: the original synthetic two-page paper completed through the
  local queue; local analysis returned 14 questions; Markdown, JSON, and DOCX
  analysis downloads succeeded.

The live run was serialized behind existing queue work and took several minutes
to complete. For a three-minute recording, pre-warm the application and record
the completed result; label any time cut honestly. Do not claim this synthetic
fixture as a real-paper benchmark.

## Three-minute demo runbook

1. **0:00–0:15 — Problem.** Show a difficult source page and a baseline text
   extraction that loses reading order, structure, or visual context.
2. **0:15–0:35 — Convert.** Open PredixaLearn, select **Exam Paper**, and use
   **Run Judge Demo** or a permitted paper. State that processing is local.
3. **0:35–1:10 — Evidence-preserving result.** Show reconstructed text,
   question hierarchy, table/figure context, source-page preview, confidence,
   and a visible warning or review item.
4. **1:10–1:50 — Past Paper Intelligence.** Open Analyze. Show a question’s
   source link, marks, topic, difficulty, and teacher-review state. Explain that
   every conclusion remains linked to OCR evidence and does not predict future
   questions.
5. **1:50–2:15 — Exports and impact.** Show Markdown, JSON, or DOCX output and
   describe how teachers can reuse source-linked material.
6. **2:15–2:40 — Measured benchmark.** Show the completed permission-cleared
   report, dataset scope, hardware, one limitation, and three failure examples.
   Omit this segment until real measurements are available.
7. **2:40–3:00 — Codex and closing.** Show completed Codex evidence and end
   with: “PredixaLearn turns difficult scans into trustworthy educational data.”

## Submission description

Past examination papers contain valuable learning material, but ordinary PDF
extraction can lose question structure, equations, tables, figures, and source
context. PredixaLearn reconstructs difficult scans locally into structured,
reviewable educational data. Its evidence-preserving pipeline retains page
references, confidence, geometry, figures, warnings, and verified structured
exports instead of silently presenting uncertain content as fact.

The optional Past Paper Intelligence layer creates teacher-reviewable,
source-linked questions, topic and marks summaries, difficulty signals, and
revision priorities. It is designed to help students and teachers study
historical papers responsibly, not to predict future exams. Sensitive documents
remain local unless a user explicitly consents to bounded cloud analysis.

## Required artefacts before upload

- Complete [Codex evidence](hackathon-codex-evidence.md) with genuine session,
  reviewer, and feature-history evidence.
- Populate and run the real-paper benchmark scaffold in
  `benchmarks/manifest.submission.template.json`; publish its measured report
  only after all source permissions and ground truth are recorded.
- Capture the frames listed in [submission-assets/README.md](submission-assets/README.md)
  and record the three-minute demonstration.
- Use an immutable commit SHA or release tag, and repeat the clean-install and
  Judge Demo smoke checks on the machine used for recording.

## Product boundaries to state plainly

- English is the only supported processing language in this release.
- GPT analysis is optional and teacher-reviewable; local OCR works without an
  API key or network.
- A successfully rendered document does not prove OCR accuracy.
- The synthetic Judge Demo is a reproducible regression fixture, not a
  real-world benchmark.
