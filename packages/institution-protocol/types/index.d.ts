export const protocolVersion: "2026-07-01";

export type Workflow = "text_recognition" | "layout_parsing" | "table_extraction" | "vl_processing";

export interface ArtifactDescriptor {
  artifact_id: string;
  kind: "source_document" | "result_json" | "markdown" | "docx" | "preview" | "crop" | "table" | "dataset_export";
  sha256: string;
  size_bytes: number;
  media_type: string;
  storage_key?: string | null;
}

export interface SyncEvent {
  event_id: string;
  event_type: "archive.upsert" | "analysis.upsert" | "review.upsert" | "artifact.publish" | "archive.delete_requested";
  occurred_at: string;
  resource_id: string;
  resource_version: number;
  idempotency_key: string;
  payload: Record<string, unknown>;
}

export interface SyncEnvelope {
  protocol_version: "2026-07-01";
  tenant_id: string;
  device_id: string;
  events: SyncEvent[];
  artifacts: ArtifactDescriptor[];
}

export interface WorkerJobEnvelope {
  protocol_version: "2026-07-01";
  job_id: string;
  tenant_id: string;
  workflow: Workflow;
  source: ArtifactDescriptor;
  settings: Record<string, unknown>;
  allowed_outputs: string[];
  expires_at: string;
}
