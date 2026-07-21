# PredixaLearn institution protocol

This package contains the versioned, strict wire contracts shared by the
standalone OCR studio, institution service, OCR workers, and webhook clients.
It intentionally contains no storage credentials, learner identity mappings,
or provider-specific implementation details.

`schemas/protocol.schema.json` and `types/index.d.ts` are the checked-in JSON
Schema and TypeScript contracts. Regenerate the schema deterministically with:

```powershell
python packages/institution-protocol/scripts/export_schemas.py
```
