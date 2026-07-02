from __future__ import annotations

import sqlite3

CANONICAL_OBJECTS = (
    "canonical_node",
    "canonical_guidance_document",
    "canonical_guidance_paragraph",
    "canonical_guidance_section",
)

CANONICAL_GUIDANCE_CREATE_SQL = r"""
CREATE TABLE canonical_guidance_document AS
WITH child_counts AS (
  SELECT
    d.id AS doc_id,
    SUM(CASE WHEN p.node_type='guidance_paragraph' AND json_extract(p.metadata_json,'$.source')='pdf_text_extraction' THEN 1 ELSE 0 END) AS pdf_paragraphs,
    SUM(CASE WHEN p.node_type='guidance_paragraph' AND coalesce(json_extract(p.metadata_json,'$.source'),'')<>'pdf_text_extraction' THEN 1 ELSE 0 END) AS html_paragraphs,
    COUNT(e.to_node_id) AS child_count
  FROM node d
  LEFT JOIN edge e ON e.from_node_id=d.id AND e.edge_type='contains'
  LEFT JOIN node p ON p.id=e.to_node_id
  WHERE d.node_type='guidance_document'
  GROUP BY d.id
), ranked AS (
  SELECT
    d.id,
    d.title,
    d.url,
    coalesce(json_extract(d.metadata_json,'$.document_type'),'') AS document_type,
    coalesce(cc.html_paragraphs,0) AS html_paragraphs,
    coalesce(cc.pdf_paragraphs,0) AS pdf_paragraphs,
    coalesce(cc.child_count,0) AS child_count,
    row_number() OVER (
      PARTITION BY d.title
      ORDER BY
        CASE WHEN coalesce(cc.html_paragraphs,0)>0 THEN 1 ELSE 0 END DESC,
        coalesce(cc.child_count,0) DESC,
        length(coalesce(d.url,'')) ASC,
        d.url ASC,
        d.id ASC
    ) AS canonical_rank
  FROM node d
  LEFT JOIN child_counts cc ON cc.doc_id=d.id
  WHERE d.node_type='guidance_document'
)
SELECT *, CASE WHEN canonical_rank=1 THEN 1 ELSE 0 END AS is_canonical
FROM ranked;
CREATE UNIQUE INDEX idx_canonical_guidance_document_id ON canonical_guidance_document(id);
CREATE INDEX idx_canonical_guidance_document_title ON canonical_guidance_document(title);
CREATE INDEX idx_canonical_guidance_document_is ON canonical_guidance_document(is_canonical);

CREATE TABLE canonical_guidance_paragraph AS
WITH doc_pref AS (
  SELECT
    json_extract(metadata_json,'$.document_title') AS document_title,
    SUM(CASE WHEN coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction' THEN 1 ELSE 0 END) AS html_paragraphs
  FROM node
  WHERE node_type='guidance_paragraph'
  GROUP BY json_extract(metadata_json,'$.document_title')
), parent_doc AS (
  SELECT
    e.to_node_id AS child_id,
    MAX(cgd.is_canonical) AS has_canonical_parent,
    MAX(CASE WHEN cgd.is_canonical=0 THEN 1 ELSE 0 END) AS has_duplicate_parent
  FROM edge e
  JOIN canonical_guidance_document cgd ON cgd.id=e.from_node_id
  WHERE e.edge_type='contains'
  GROUP BY e.to_node_id
)
SELECT
  p.id,
  json_extract(p.metadata_json,'$.document_title') AS document_title,
  coalesce(json_extract(p.metadata_json,'$.source'),'html') AS source_preference_source,
  coalesce(dp.html_paragraphs,0) AS document_html_paragraphs,
  coalesce(pd.has_canonical_parent,1) AS has_canonical_parent,
  coalesce(pd.has_duplicate_parent,0) AS has_duplicate_parent,
  CASE
    WHEN coalesce(pd.has_canonical_parent,1)=0 AND coalesce(pd.has_duplicate_parent,0)=1 THEN 0
    WHEN coalesce(dp.html_paragraphs,0)>0 AND coalesce(json_extract(p.metadata_json,'$.source'),'')='pdf_text_extraction' THEN 0
    ELSE 1
  END AS is_canonical
FROM node p
LEFT JOIN doc_pref dp ON dp.document_title=json_extract(p.metadata_json,'$.document_title')
LEFT JOIN parent_doc pd ON pd.child_id=p.id
WHERE p.node_type='guidance_paragraph';
CREATE UNIQUE INDEX idx_canonical_guidance_paragraph_id ON canonical_guidance_paragraph(id);
CREATE INDEX idx_canonical_guidance_paragraph_doc ON canonical_guidance_paragraph(document_title);
CREATE INDEX idx_canonical_guidance_paragraph_is ON canonical_guidance_paragraph(is_canonical);

CREATE TABLE canonical_guidance_section AS
WITH doc_pref AS (
  SELECT
    json_extract(metadata_json,'$.document_title') AS document_title,
    SUM(CASE WHEN coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction' THEN 1 ELSE 0 END) AS html_paragraphs
  FROM node
  WHERE node_type='guidance_paragraph'
  GROUP BY json_extract(metadata_json,'$.document_title')
), parent_doc AS (
  SELECT
    e.to_node_id AS child_id,
    MAX(cgd.is_canonical) AS has_canonical_parent,
    MAX(CASE WHEN cgd.is_canonical=0 THEN 1 ELSE 0 END) AS has_duplicate_parent
  FROM edge e
  JOIN canonical_guidance_document cgd ON cgd.id=e.from_node_id
  WHERE e.edge_type='contains'
  GROUP BY e.to_node_id
)
SELECT
  s.id,
  json_extract(s.metadata_json,'$.document_title') AS document_title,
  coalesce(json_extract(s.metadata_json,'$.source'),'html') AS source_preference_source,
  coalesce(dp.html_paragraphs,0) AS document_html_paragraphs,
  coalesce(pd.has_canonical_parent,1) AS has_canonical_parent,
  coalesce(pd.has_duplicate_parent,0) AS has_duplicate_parent,
  CASE
    WHEN coalesce(pd.has_canonical_parent,1)=0 AND coalesce(pd.has_duplicate_parent,0)=1 THEN 0
    WHEN coalesce(dp.html_paragraphs,0)>0 AND coalesce(json_extract(s.metadata_json,'$.source'),'')='pdf_text_extraction' THEN 0
    ELSE 1
  END AS is_canonical
FROM node s
LEFT JOIN doc_pref dp ON dp.document_title=json_extract(s.metadata_json,'$.document_title')
LEFT JOIN parent_doc pd ON pd.child_id=s.id
WHERE s.node_type='guidance_section';
CREATE UNIQUE INDEX idx_canonical_guidance_section_id ON canonical_guidance_section(id);
CREATE INDEX idx_canonical_guidance_section_doc ON canonical_guidance_section(document_title);
CREATE INDEX idx_canonical_guidance_section_is ON canonical_guidance_section(is_canonical);

CREATE TABLE canonical_node AS
SELECT
  n.id,
  CASE
    WHEN n.node_type='guidance_document' THEN coalesce(cgd.is_canonical,0)
    WHEN n.node_type='guidance_paragraph' THEN coalesce(cgp.is_canonical,1)
    WHEN n.node_type='guidance_section' THEN coalesce(cgs.is_canonical,1)
    ELSE 1
  END AS is_canonical,
  CASE
    WHEN n.node_type='guidance_document' AND coalesce(cgd.is_canonical,0)=0 THEN 'duplicate_guidance_document_title'
    WHEN n.node_type='guidance_paragraph' AND coalesce(cgp.has_canonical_parent,1)=0 AND coalesce(cgp.has_duplicate_parent,0)=1 THEN 'duplicate_guidance_document_child'
    WHEN n.node_type='guidance_section' AND coalesce(cgs.has_canonical_parent,1)=0 AND coalesce(cgs.has_duplicate_parent,0)=1 THEN 'duplicate_guidance_document_child'
    WHEN n.node_type='guidance_paragraph' AND coalesce(cgp.is_canonical,1)=0 THEN 'pdf_suppressed_html_preferred'
    WHEN n.node_type='guidance_section' AND coalesce(cgs.is_canonical,1)=0 THEN 'pdf_suppressed_html_preferred'
    ELSE 'canonical'
  END AS canonical_reason
FROM node n
LEFT JOIN canonical_guidance_document cgd ON cgd.id=n.id
LEFT JOIN canonical_guidance_paragraph cgp ON cgp.id=n.id
LEFT JOIN canonical_guidance_section cgs ON cgs.id=n.id;
CREATE UNIQUE INDEX idx_canonical_node_id ON canonical_node(id);
CREATE INDEX idx_canonical_node_is ON canonical_node(is_canonical);
CREATE INDEX idx_canonical_node_reason ON canonical_node(canonical_reason);
"""


def rebuild_canonical_guidance(conn: sqlite3.Connection) -> None:
    """Rebuild materialised canonical guidance tables.

    Older builds used SQL views with the same names. SQLite deliberately errors
    if DROP TABLE is used on a view or DROP VIEW is used on a table, so inspect
    sqlite_master before dropping.
    """
    for name in CANONICAL_OBJECTS:
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name=? AND type IN ('table','view')",
            (name,),
        ).fetchone()
        if row:
            object_type = row[0]
            if object_type == "table":
                conn.execute(f"DROP TABLE {name}")
            elif object_type == "view":
                conn.execute(f"DROP VIEW {name}")
    conn.executescript(CANONICAL_GUIDANCE_CREATE_SQL)
