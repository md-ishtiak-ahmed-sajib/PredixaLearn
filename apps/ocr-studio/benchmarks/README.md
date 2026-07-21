# PredixaLearn Benchmark Protocol

The bundled synthetic Judge Demo is a regression fixture, not a real-world accuracy claim. Its expected question/mark/feature evidence is checked in at `../tests/fixtures/education/judge-demo.expected.json`. Add only source papers that you are permitted to process, distribute, and benchmark.

1. Copy `manifest.example.json` and document source permission, hardware, and software versions.
2. Create manually verified ground truth for each permitted case.
3. Save PredixaLearn result JSON files as `<case_id>.json` in a separate results directory.
4. Run:

```powershell
python scripts/run_benchmark.py benchmarks/manifest.json path/to/results --output benchmark-report.json
```

The manifest accepts optional `features.tables`, `features.figures`, and `features.equations` counts. Result JSON may report `processing_time_ms`, `correction_time_ms`, and source-linked AI suggestions. The report calculates character/word error, question-number and reading-order accuracy, table/equation/figure handling, source traceability, processing time, correction time, and AI-grounding rate where those values exist. Include failures and limitations; do not publish unmeasured values.
