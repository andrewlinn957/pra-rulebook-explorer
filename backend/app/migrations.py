from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


LATEST_SCHEMA_VERSION = 7


ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS reporting_template_enrichment (
  template_id TEXT PRIMARY KEY,
  graph_node_id TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  purpose TEXT,
  contents TEXT,
  summary TEXT,
  key_rows_json TEXT NOT NULL DEFAULT '[]',
  quality_notes TEXT,
  response_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (graph_node_id) REFERENCES graph_node(node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reporting_template_enrichment_status
  ON reporting_template_enrichment(status);
CREATE INDEX IF NOT EXISTS idx_reporting_template_enrichment_prompt
  ON reporting_template_enrichment(prompt_version,input_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reporting_template_enrichment_graph_node
  ON reporting_template_enrichment(graph_node_id);
"""


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply ordered, idempotent schema migrations to the live reporting store."""
    current = schema_version(conn)
    if current > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported version {LATEST_SCHEMA_VERSION}"
        )
    applied: list[int] = []
    if current < 1:
        _migrate_v1(conn)
        applied.append(1)
        current = 1
    if current < 2:
        _migrate_v2(conn)
        applied.append(2)
        current = 2
    if current < 3:
        _migrate_v3(conn)
        applied.append(3)
        current = 3
    if current < 4:
        _migrate_v4(conn)
        applied.append(4)
        current = 4
    if current < 5:
        _migrate_v5(conn)
        applied.append(5)
        current = 5
    if current < 6:
        _migrate_v6(conn)
        applied.append(6)
        current = 6
    if current < 7:
        _migrate_v7(conn)
        applied.append(7)
    return applied


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Store every legal citation span independently from its graph edge."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reference_occurrence (
          occurrence_id TEXT PRIMARY KEY,
          group_id TEXT NOT NULL,
          source_node_id TEXT NOT NULL,
          target_node_id TEXT,
          edge_id TEXT,
          relationship_type TEXT NOT NULL DEFAULT 'REF',
          citation_kind TEXT NOT NULL,
          citation_text TEXT NOT NULL,
          group_text TEXT NOT NULL,
          instrument_id TEXT,
          provision_path TEXT,
          qualifier TEXT DEFAULT '',
          span_start INTEGER NOT NULL,
          span_end INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'materialized','unresolved','ambiguous','not_reference'
          )),
          source_method TEXT NOT NULL,
          confidence REAL NOT NULL,
          context_text TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          CHECK(span_start >= 0 AND span_end >= span_start),
          CHECK(confidence >= 0.0 AND confidence <= 1.0),
          CHECK(json_valid(metadata_json))
        );
        CREATE INDEX IF NOT EXISTS idx_reference_occurrence_source
          ON reference_occurrence(source_node_id,span_start,span_end);
        CREATE INDEX IF NOT EXISTS idx_reference_occurrence_target
          ON reference_occurrence(target_node_id);
        CREATE INDEX IF NOT EXISTS idx_reference_occurrence_edge
          ON reference_occurrence(edge_id);
        CREATE INDEX IF NOT EXISTS idx_reference_occurrence_status
          ON reference_occurrence(status);
        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (7,'legal_reference_occurrences',CURRENT_TIMESTAMP);
        PRAGMA user_version=7;
        """
    )
    conn.commit()


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Persist human-readable name overrides across ontology rebuilds."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reporting_display_name_override (
          entity_type TEXT NOT NULL CHECK(entity_type IN (
            'regime','collection','requirement','edition','resource','component','taxonomy_release'
          )),
          entity_id TEXT NOT NULL,
          display_name TEXT NOT NULL CHECK(trim(display_name) <> ''),
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(entity_type,entity_id)
        );
        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (6,'durable_reporting_display_name_overrides',CURRENT_TIMESTAMP);
        PRAGMA user_version=6;
        """
    )
    conn.commit()


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Add contextual human-readable names with requirement inheritance."""
    for table in (
        "reporting_regime", "reporting_collection", "reporting_requirement",
        "reporting_requirement_edition", "reporting_resource",
        "reporting_resource_component", "reporting_taxonomy_release",
    ):
        if "display_name" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN display_name TEXT")
    conn.executescript(
        """
        DROP VIEW IF EXISTS reporting_requirement_names;
        CREATE VIEW reporting_requirement_names AS
        SELECT r.requirement_id,
               COALESCE(NULLIF(r.display_name,''), r.code || ' — ' || r.name) AS resolved_display_name,
               CASE WHEN NULLIF(r.display_name,'') IS NOT NULL THEN 'requirement_override' ELSE 'requirement_code_and_name' END AS display_name_source
        FROM reporting_requirement r;

        DROP VIEW IF EXISTS reporting_edition_names;
        CREATE VIEW reporting_edition_names AS
        SELECT e.edition_id,e.requirement_id,
               COALESCE(NULLIF(e.display_name,''), rn.resolved_display_name) AS resolved_display_name,
               CASE WHEN NULLIF(e.display_name,'') IS NOT NULL THEN 'edition_override' ELSE 'inherited_from_requirement' END AS display_name_source
        FROM reporting_requirement_edition e
        JOIN reporting_requirement_names rn ON rn.requirement_id=e.requirement_id;

        DROP VIEW IF EXISTS reporting_edition_resource_names;
        CREATE VIEW reporting_edition_resource_names AS
        SELECT er.edition_id,er.resource_id,
               COALESCE(
                 NULLIF(res.display_name,''),
                 en.resolved_display_name || ' — ' ||
                   CASE er.relationship
                     WHEN 'template' THEN 'Reporting template'
                     WHEN 'instructions' THEN 'Reporting instructions'
                     WHEN 'guidance' THEN 'Guidance'
                     WHEN 'schedule' THEN 'Reporting schedule'
                     ELSE 'Resource'
                   END
               ) AS resolved_display_name,
               CASE WHEN NULLIF(res.display_name,'') IS NOT NULL THEN 'resource_override' ELSE 'inherited_from_requirement' END AS display_name_source,
               en.resolved_display_name AS inherited_requirement_name
        FROM reporting_edition_resource er
        JOIN reporting_resource res ON res.resource_id=er.resource_id
        JOIN reporting_edition_names en ON en.edition_id=er.edition_id;

        DROP VIEW IF EXISTS reporting_component_names;
        CREATE VIEW reporting_component_names AS
        SELECT c.component_id,c.resource_id,
               COALESCE(
                 NULLIF(c.display_name,''),
                 NULLIF(res.display_name,''),
                 c.name
               ) AS resolved_display_name,
               CASE
                 WHEN NULLIF(c.display_name,'') IS NOT NULL THEN 'component_override'
                 WHEN NULLIF(res.display_name,'') IS NOT NULL THEN 'inherited_from_resource'
                 ELSE 'component_name'
               END AS display_name_source
        FROM reporting_resource_component c
        JOIN reporting_resource res ON res.resource_id=c.resource_id;

        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (5,'reporting_human_readable_name_inheritance',CURRENT_TIMESTAMP);
        PRAGMA user_version=5;
        """
    )
    conn.commit()


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Install the normalized PRA reporting-estate ontology."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reporting_regime (
          regime_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          description TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reporting_collection (
          collection_id TEXT PRIMARY KEY,
          regime_id TEXT NOT NULL,
          name TEXT NOT NULL,
          description TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          UNIQUE(regime_id,name),
          FOREIGN KEY(regime_id) REFERENCES reporting_regime(regime_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reporting_requirement (
          requirement_id TEXT PRIMARY KEY,
          collection_id TEXT NOT NULL,
          requirement_type TEXT NOT NULL CHECK(requirement_type IN ('regulatory_return','disclosure_requirement')),
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          description TEXT,
          subject_tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(subject_tags_json)),
          UNIQUE(collection_id,code),
          FOREIGN KEY(collection_id) REFERENCES reporting_collection(collection_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reporting_requirement_edition (
          edition_id TEXT PRIMARY KEY,
          requirement_id TEXT NOT NULL,
          official_name TEXT NOT NULL,
          description TEXT,
          effective_from TEXT,
          effective_to TEXT,
          effective_text TEXT,
          status TEXT NOT NULL CHECK(status IN ('future','current','superseded')),
          source_page_url TEXT NOT NULL,
          legacy_return_id TEXT UNIQUE,
          FOREIGN KEY(requirement_id) REFERENCES reporting_requirement(requirement_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_requirement_edition_requirement
          ON reporting_requirement_edition(requirement_id,status,effective_from);

        CREATE TABLE IF NOT EXISTS reporting_resource (
          resource_id TEXT PRIMARY KEY,
          source_id TEXT,
          resource_role TEXT NOT NULL,
          file_format TEXT,
          title TEXT NOT NULL,
          description TEXT,
          url TEXT NOT NULL,
          legacy_artifact_id TEXT UNIQUE,
          FOREIGN KEY(source_id) REFERENCES source_document(source_id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_resource_role
          ON reporting_resource(resource_role,file_format);

        CREATE TABLE IF NOT EXISTS reporting_edition_resource (
          edition_id TEXT NOT NULL,
          resource_id TEXT NOT NULL,
          relationship TEXT NOT NULL CHECK(relationship IN ('template','instructions','guidance','schedule')),
          is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0,1)),
          display_order INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(edition_id,resource_id,relationship),
          FOREIGN KEY(edition_id) REFERENCES reporting_requirement_edition(edition_id) ON DELETE CASCADE,
          FOREIGN KEY(resource_id) REFERENCES reporting_resource(resource_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reporting_resource_component (
          component_id TEXT PRIMARY KEY,
          resource_id TEXT NOT NULL,
          parent_component_id TEXT,
          component_type TEXT NOT NULL CHECK(component_type IN (
            'worksheet','logical_template','instruction_section','instruction_provision',
            'taxonomy_entry_point','table_definition','concept','dimension','validation_rule','sample_instance'
          )),
          component_role TEXT,
          code TEXT,
          name TEXT NOT NULL,
          sort_order INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
          FOREIGN KEY(resource_id) REFERENCES reporting_resource(resource_id) ON DELETE CASCADE,
          FOREIGN KEY(parent_component_id) REFERENCES reporting_resource_component(component_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_resource_component_resource
          ON reporting_resource_component(resource_id,component_type,sort_order);

        CREATE TABLE IF NOT EXISTS reporting_taxonomy_release (
          release_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          version TEXT NOT NULL UNIQUE,
          description TEXT
        );

        CREATE TABLE IF NOT EXISTS reporting_taxonomy_resource (
          release_id TEXT NOT NULL,
          resource_id TEXT NOT NULL,
          relationship TEXT NOT NULL,
          PRIMARY KEY(release_id,resource_id,relationship),
          FOREIGN KEY(release_id) REFERENCES reporting_taxonomy_release(release_id) ON DELETE CASCADE,
          FOREIGN KEY(resource_id) REFERENCES reporting_resource(resource_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reporting_edition_taxonomy (
          edition_id TEXT NOT NULL,
          release_id TEXT NOT NULL,
          relationship TEXT NOT NULL DEFAULT 'supported_by',
          evidence TEXT,
          PRIMARY KEY(edition_id,release_id),
          FOREIGN KEY(edition_id) REFERENCES reporting_requirement_edition(edition_id) ON DELETE CASCADE,
          FOREIGN KEY(release_id) REFERENCES reporting_taxonomy_release(release_id) ON DELETE CASCADE
        );

        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (4,'pra_reporting_estate_ontology',CURRENT_TIMESTAMP);
        PRAGMA user_version=4;
        """
    )
    conn.commit()


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Track catalog semantic enrichment without exposing its audit plumbing."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reporting_catalog_enrichment (
          return_id TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          response_json TEXT,
          error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (return_id) REFERENCES reporting_return_catalog(return_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_catalog_enrichment_status
          ON reporting_catalog_enrichment(status,prompt_version);
        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (3,'reporting_catalog_semantic_enrichment',CURRENT_TIMESTAMP);
        PRAGMA user_version=3;
        """
    )
    conn.commit()


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Install the reporting-estate catalogue used by the public UI.

    The catalogue deliberately separates a return from its files.  A workbook
    can therefore be a Pillar 3 disclosure template without accidentally
    becoming a COREP return, and an exceptional PDF form can still be a
    template when the authoritative PRA page says that it is one.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reporting_return_catalog (
          return_id TEXT PRIMARY KEY,
          return_code TEXT NOT NULL,
          name TEXT NOT NULL,
          description TEXT,
          estate TEXT NOT NULL,
          family TEXT,
          effective_from TEXT,
          effective_to TEXT,
          effective_text TEXT,
          source_page_url TEXT NOT NULL,
          source_table INTEGER,
          source_row INTEGER,
          status TEXT NOT NULL DEFAULT 'current',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_return_catalog_code
          ON reporting_return_catalog(return_code);
        CREATE INDEX IF NOT EXISTS idx_reporting_return_catalog_estate
          ON reporting_return_catalog(estate,family,return_code);

        CREATE TABLE IF NOT EXISTS reporting_artifact (
          artifact_id TEXT PRIMARY KEY,
          source_id TEXT,
          url TEXT NOT NULL,
          display_title TEXT NOT NULL,
          artifact_role TEXT NOT NULL,
          estate TEXT NOT NULL,
          file_type TEXT,
          sheet_names_json TEXT NOT NULL DEFAULT '[]',
          extracted_title TEXT,
          description TEXT,
          taxonomy_version TEXT,
          classification_method TEXT NOT NULL,
          classification_confidence REAL NOT NULL DEFAULT 1.0,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          CHECK (sheet_names_json IS NULL OR json_valid(sheet_names_json)),
          CHECK (classification_confidence >= 0.0 AND classification_confidence <= 1.0),
          FOREIGN KEY (source_id) REFERENCES source_document(source_id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reporting_artifact_url_role
          ON reporting_artifact(url,artifact_role);
        CREATE INDEX IF NOT EXISTS idx_reporting_artifact_role
          ON reporting_artifact(estate,artifact_role);

        CREATE TABLE IF NOT EXISTS reporting_return_artifact (
          return_id TEXT NOT NULL,
          artifact_id TEXT NOT NULL,
          relationship TEXT NOT NULL,
          is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0,1)),
          display_order INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (return_id,artifact_id,relationship),
          FOREIGN KEY (return_id) REFERENCES reporting_return_catalog(return_id) ON DELETE CASCADE,
          FOREIGN KEY (artifact_id) REFERENCES reporting_artifact(artifact_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reporting_return_artifact_return
          ON reporting_return_artifact(return_id,relationship,display_order);

        INSERT OR REPLACE INTO schema_migration(version,name,applied_at)
        VALUES (2,'normalise_reporting_estate_catalogue',CURRENT_TIMESTAMP);
        PRAGMA user_version=2;
        """
    )
    conn.commit()


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Normalize enrichment ownership and install projection invalidation state."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )

        has_graph = _has_table(conn, "graph_node")
        has_enrichment = _has_table(conn, "reporting_template_enrichment")
        if has_graph and has_enrichment and "graph_node_id" not in _columns(conn, "reporting_template_enrichment"):
            unmatched = conn.execute(
                """
                SELECT COUNT(*)
                FROM reporting_template_enrichment e
                WHERE NOT EXISTS (
                  SELECT 1 FROM graph_node n
                  WHERE n.node_type='Template'
                    AND (n.node_id=e.template_id OR n.source_pk=e.template_id)
                )
                """
            ).fetchone()[0]
            if unmatched:
                raise RuntimeError(f"cannot migrate {unmatched} template enrichments without graph templates")
            conn.execute("ALTER TABLE reporting_template_enrichment RENAME TO reporting_template_enrichment_v0")
            conn.executescript(ENRICHMENT_SCHEMA)
            conn.execute(
                """
                INSERT INTO reporting_template_enrichment(
                  template_id,graph_node_id,model,prompt_version,input_hash,status,
                  purpose,contents,summary,key_rows_json,quality_notes,response_json,error,
                  created_at,updated_at
                )
                SELECT e.template_id,
                       COALESCE(
                         (SELECT n.node_id FROM graph_node n
                          WHERE n.node_type='Template' AND n.node_id=e.template_id LIMIT 1),
                         (SELECT n.node_id FROM graph_node n
                          WHERE n.node_type='Template' AND n.source_pk=e.template_id
                          ORDER BY n.node_id LIMIT 1)
                       ),
                       e.model,e.prompt_version,e.input_hash,e.status,
                       e.purpose,e.contents,e.summary,e.key_rows_json,e.quality_notes,
                       e.response_json,e.error,e.created_at,e.updated_at
                FROM reporting_template_enrichment_v0 e
                """
            )
            conn.execute("DROP TABLE reporting_template_enrichment_v0")
        elif has_graph and not has_enrichment:
            conn.executescript(ENRICHMENT_SCHEMA)

        if _has_table(conn, "graph_edge") and _has_table(conn, "source_span"):
            conn.execute(
                """
                UPDATE graph_edge SET evidence_span_id=NULL
                WHERE evidence_span_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM source_span s WHERE s.span_id=graph_edge.evidence_span_id)
                """
            )

        if _has_table(conn, "node"):
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_projection_state (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
                  refreshed_at TEXT
                );
                INSERT OR IGNORE INTO search_projection_state(singleton,dirty) VALUES (1,1);
                CREATE TRIGGER IF NOT EXISTS trg_node_search_dirty_insert
                AFTER INSERT ON node BEGIN
                  UPDATE search_projection_state SET dirty=1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_node_search_dirty_update
                AFTER UPDATE ON node BEGIN
                  UPDATE search_projection_state SET dirty=1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_node_search_dirty_delete
                AFTER DELETE ON node BEGIN
                  UPDATE search_projection_state SET dirty=1 WHERE singleton=1;
                END;
                """
            )

        conn.execute(
            "INSERT OR REPLACE INTO schema_migration(version,name,applied_at) VALUES (?,?,?)",
            (1, "stabilize_integrity_and_search_projections", datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
