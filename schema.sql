-- PRA Rulebook Explorer reporting ingestion schema
-- Target: SQLite 3.x
-- Purpose: store raw source metadata, parsed spans, reporting templates/datapoints,
-- instructions, permissions, calculations/validations, and graph projections.
--
-- This schema is intentionally additive and does not modify the existing rulebook
-- node/edge tables. Apply only when ready to create the reporting pipeline tables.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_document (
  source_id TEXT PRIMARY KEY,
  title TEXT,
  url TEXT NOT NULL,
  local_path TEXT,
  file_type TEXT,
  checksum_sha256 TEXT,
  downloaded_at TEXT,
  publication_date TEXT,
  effective_from TEXT,
  effective_to TEXT,
  parent_url TEXT,
  source_status TEXT NOT NULL DEFAULT 'downloaded',
  notes TEXT
);

CREATE TABLE IF NOT EXISTS source_span (
  span_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  span_type TEXT NOT NULL,
  page_number INTEGER,
  sheet_name TEXT,
  row_number INTEGER,
  column_number INTEGER,
  heading_path TEXT,
  anchor TEXT,
  raw_text TEXT,
  normalised_text TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  FOREIGN KEY (source_id) REFERENCES source_document(source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rulebook_part (
  part_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT,
  firm_type TEXT,
  effective_from TEXT,
  effective_to TEXT
);

CREATE TABLE IF NOT EXISTS provision (
  provision_id TEXT PRIMARY KEY,
  part_id TEXT NOT NULL,
  provision_label TEXT NOT NULL,
  provision_type TEXT,
  heading_path TEXT,
  text TEXT,
  effective_from TEXT,
  effective_to TEXT,
  source_span_id TEXT,
  FOREIGN KEY (part_id) REFERENCES rulebook_part(part_id) ON DELETE CASCADE,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS reporting_obligation (
  obligation_id TEXT PRIMARY KEY,
  data_item_code TEXT NOT NULL,
  title TEXT,
  domain TEXT,
  frequency TEXT,
  reporting_horizon_days INTEGER,
  effective_from TEXT,
  effective_to TEXT,
  source_span_id TEXT,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS template (
  template_id TEXT PRIMARY KEY,
  template_code TEXT NOT NULL,
  title TEXT,
  annex TEXT,
  source_id TEXT,
  effective_from TEXT,
  effective_to TEXT,
  FOREIGN KEY (source_id) REFERENCES source_document(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS template_row (
  row_id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL,
  row_code TEXT,
  row_order INTEGER,
  parent_row_id TEXT,
  label TEXT,
  source_span_id TEXT,
  FOREIGN KEY (template_id) REFERENCES template(template_id) ON DELETE CASCADE,
  FOREIGN KEY (parent_row_id) REFERENCES template_row(row_id) ON DELETE SET NULL,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS template_column (
  column_id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL,
  column_code TEXT,
  column_order INTEGER,
  label TEXT,
  unit_type TEXT,
  source_span_id TEXT,
  FOREIGN KEY (template_id) REFERENCES template(template_id) ON DELETE CASCADE,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS datapoint (
  datapoint_id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL,
  row_id TEXT,
  column_id TEXT,
  data_type TEXT,
  unit_type TEXT,
  concept_label TEXT,
  source_span_id TEXT,
  FOREIGN KEY (template_id) REFERENCES template(template_id) ON DELETE CASCADE,
  FOREIGN KEY (row_id) REFERENCES template_row(row_id) ON DELETE SET NULL,
  FOREIGN KEY (column_id) REFERENCES template_column(column_id) ON DELETE SET NULL,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS instruction (
  instruction_id TEXT PRIMARY KEY,
  instruction_set TEXT,
  applies_to_type TEXT NOT NULL,
  applies_to_id TEXT NOT NULL,
  text TEXT NOT NULL,
  source_span_id TEXT,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS concept (
  concept_id TEXT PRIMARY KEY,
  concept_type TEXT,
  label TEXT NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS permission (
  permission_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  provision_id TEXT,
  permission_type TEXT,
  description TEXT,
  source_span_id TEXT,
  FOREIGN KEY (provision_id) REFERENCES provision(provision_id) ON DELETE SET NULL,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS calculation_rule (
  calculation_id TEXT PRIMARY KEY,
  label TEXT,
  expression_text TEXT,
  expression_json TEXT,
  source_span_id TEXT,
  CHECK (expression_json IS NULL OR json_valid(expression_json)),
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS validation_rule (
  validation_id TEXT PRIMARY KEY,
  label TEXT,
  expression_text TEXT,
  expression_json TEXT,
  source_id TEXT,
  source_span_id TEXT,
  CHECK (expression_json IS NULL OR json_valid(expression_json)),
  FOREIGN KEY (source_id) REFERENCES source_document(source_id) ON DELETE SET NULL,
  FOREIGN KEY (source_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS graph_node (
  node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  label TEXT,
  source_table TEXT,
  source_pk TEXT,
  properties_json TEXT,
  effective_from TEXT,
  effective_to TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  CHECK (properties_json IS NULL OR json_valid(properties_json))
);

CREATE TABLE IF NOT EXISTS graph_edge (
  edge_id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  properties_json TEXT,
  evidence_span_id TEXT,
  confidence REAL,
  extraction_method TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  effective_from TEXT,
  effective_to TEXT,
  CHECK (properties_json IS NULL OR json_valid(properties_json)),
  CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  FOREIGN KEY (source_node_id) REFERENCES graph_node(node_id) ON DELETE CASCADE,
  FOREIGN KEY (target_node_id) REFERENCES graph_node(node_id) ON DELETE CASCADE,
  FOREIGN KEY (evidence_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
);

-- Deterministic uniqueness constraints for common ingestion keys.
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_document_url_checksum
  ON source_document(url, checksum_sha256);

CREATE UNIQUE INDEX IF NOT EXISTS ux_template_code_effective
  ON template(template_code, COALESCE(effective_from, ''), COALESCE(effective_to, ''));

CREATE UNIQUE INDEX IF NOT EXISTS ux_template_row_code
  ON template_row(template_id, COALESCE(row_code, ''));

CREATE UNIQUE INDEX IF NOT EXISTS ux_template_column_code
  ON template_column(template_id, COALESCE(column_code, ''));

CREATE UNIQUE INDEX IF NOT EXISTS ux_graph_node_projection
  ON graph_node(source_table, source_pk)
  WHERE source_table IS NOT NULL AND source_pk IS NOT NULL;

-- Required indexes.
CREATE INDEX IF NOT EXISTS idx_source_document_url
  ON source_document(url);

CREATE INDEX IF NOT EXISTS idx_source_document_checksum_sha256
  ON source_document(checksum_sha256);

CREATE INDEX IF NOT EXISTS idx_provision_part_id
  ON provision(part_id);

CREATE INDEX IF NOT EXISTS idx_provision_provision_label
  ON provision(provision_label);

CREATE INDEX IF NOT EXISTS idx_template_template_code
  ON template(template_code);

CREATE INDEX IF NOT EXISTS idx_datapoint_template_id
  ON datapoint(template_id);

CREATE INDEX IF NOT EXISTS idx_graph_node_node_type
  ON graph_node(node_type);

CREATE INDEX IF NOT EXISTS idx_graph_edge_source_node_id
  ON graph_edge(source_node_id);

CREATE INDEX IF NOT EXISTS idx_graph_edge_target_node_id
  ON graph_edge(target_node_id);

CREATE INDEX IF NOT EXISTS idx_graph_edge_edge_type
  ON graph_edge(edge_type);

-- Additional foreign-key lookup indexes useful for ingestion and review workflows.
CREATE INDEX IF NOT EXISTS idx_source_span_source_id
  ON source_span(source_id);

CREATE INDEX IF NOT EXISTS idx_template_row_template_id
  ON template_row(template_id);

CREATE INDEX IF NOT EXISTS idx_template_column_template_id
  ON template_column(template_id);

CREATE INDEX IF NOT EXISTS idx_instruction_applies_to
  ON instruction(applies_to_type, applies_to_id);

CREATE INDEX IF NOT EXISTS idx_permission_provision_id
  ON permission(provision_id);

CREATE INDEX IF NOT EXISTS idx_validation_rule_source_id
  ON validation_rule(source_id);
