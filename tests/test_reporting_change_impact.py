import sqlite3
import unittest
import json
from pathlib import Path

from backend.app.migrations import apply_migrations
from backend.app.reporting import reporting_change_impact


ROOT = Path(__file__).resolve().parents[1]


class ReportingChangeImpactTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text())
        apply_migrations(self.conn)
        self.conn.execute(
            """
            INSERT INTO source_document(source_id,title,url,file_type)
            VALUES (
              'instructions','PRA115 instructions',
              'https://example.test/pra115-instructions.pdf','pdf'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO source_span(span_id,source_id,span_type,raw_text,page_number)
            VALUES (
              'rule-reference','instructions','paragraph',
              'Report the exposure in accordance with Article 4.',12
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk)
            VALUES
              ('provision:article-4','Provision','Article 4','node','article-4'),
              ('data_item:PRA115','DataItem','PRA115','reporting_obligation','data_item:PRA115'),
              ('source:instructions','SourceDocument','PRA115 instructions','source_document','instructions'),
              ('template:PRA115','Template','PRA115 template','template','template:PRA115'),
              ('datapoint:PRA115:r010:c020','DataPoint','Step-in exposure','datapoint','datapoint:PRA115:r010:c020')
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,evidence_span_id,
              confidence,extraction_method
            ) VALUES
              ('evidence','data_item:PRA115','source:instructions','EVIDENCED_BY',NULL,1,'manifest'),
              ('reference','source:instructions','provision:article-4','REFERENCES_RULE','rule-reference',.95,'reporting_llm_reference'),
              ('uses','data_item:PRA115','template:PRA115','USES_TEMPLATE',NULL,1,'manifest'),
              ('cell','template:PRA115','datapoint:PRA115:r010:c020','HAS_DATAPOINT',NULL,1,'manifest')
            """
        )
        self.conn.execute(
            """
            INSERT INTO template(template_id,template_code,title,source_id)
            VALUES ('template:PRA115','PRA115','Step-in risk','instructions')
            """
        )
        self.conn.execute(
            """
            INSERT INTO template_row(row_id,template_id,row_code,row_order,label)
            VALUES ('row:PRA115:010','template:PRA115','010',1,'Exposure')
            """
        )
        self.conn.execute(
            """
            INSERT INTO template_column(column_id,template_id,column_code,column_order,label)
            VALUES ('column:PRA115:020','template:PRA115','020',1,'Amount')
            """
        )
        self.conn.execute(
            """
            INSERT INTO datapoint(
              datapoint_id,template_id,row_id,column_id,concept_label
            ) VALUES (
              'datapoint:PRA115:r010:c020','template:PRA115',
              'row:PRA115:010','column:PRA115:020','Step-in exposure'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO instruction(
              instruction_id,instruction_set,applies_to_type,applies_to_id,text,
              source_span_id
            ) VALUES (
              'instruction:pra115','PRA115 instructions','template',
              'template:PRA115','Report the exposure.','rule-reference'
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_impact_separates_direct_instruction_evidence_from_candidate_cells(self):
        result = reporting_change_impact(
            self.conn,
            "provision:article-4",
            sample_cells=5,
        )

        self.assertEqual(result["counts"]["affected_returns"], 1)
        self.assertEqual(result["counts"]["direct_references"], 1)
        self.assertEqual(result["counts"]["candidate_templates"], 1)
        self.assertEqual(result["counts"]["candidate_cells"], 1)
        impacted = result["returns"][0]
        self.assertEqual(impacted["impact_tier"], "direct_instruction_reference")
        self.assertEqual(impacted["references"][0]["page_number"], 12)
        self.assertEqual(impacted["templates"][0]["impact_tier"], "candidate_scope")
        self.assertEqual(
            impacted["candidate_cell_samples"][0]["impact_tier"],
            "candidate_scope",
        )
        self.assertIn("does not yet prove", result["impact_model"]["candidate_scope"])

    def test_unknown_target_returns_none(self):
        self.assertIsNone(reporting_change_impact(self.conn, "provision:missing"))

    def test_same_instruction_rule_and_cell_is_direct_coordinate_evidence(self):
        self.conn.execute(
            """
            INSERT INTO graph_node(
              node_id,node_type,label,source_table,source_pk,properties_json
            ) VALUES (
              'instruction_provision:pra115','InstructionProvision',
              'Report row 010 column 020 in accordance with Article 4.',
              'instruction_projection','instruction:pra115',?
            )
            """,
            (
                json.dumps(
                    {
                        "text": (
                            "Report row 010 column 020 in accordance with "
                            "Article 4."
                        )
                    }
                ),
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,properties_json,
              evidence_span_id,confidence,extraction_method
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "instruction-evidence",
                    "instruction_provision:pra115",
                    "source:instructions",
                    "EVIDENCED_BY",
                    "{}",
                    "rule-reference",
                    1.0,
                    "instruction_coordinate_projection",
                ),
                (
                    "instruction-rule",
                    "instruction_provision:pra115",
                    "provision:article-4",
                    "REFERENCES_RULE",
                    json.dumps(
                        {
                            "canonical_key": "article:regulatory:4",
                            "reference_label": "Regulatory Article 4",
                        }
                    ),
                    "rule-reference",
                    0.96,
                    "instruction_legal_reference_projection",
                ),
                (
                    "instruction-cell",
                    "instruction_provision:pra115",
                    "datapoint:PRA115:r010:c020",
                    "INSTRUCTS",
                    json.dumps(
                        {
                            "coordinate_relation": (
                                "normative_reporting_coordinate"
                            ),
                            "coordinate_evidence": "row 010 column 020",
                            "template_id": "template:PRA115",
                            "template_code": "PRA115",
                            "row_code": "010",
                            "column_code": "020",
                        }
                    ),
                    "rule-reference",
                    0.99,
                    "instruction_coordinate_projection",
                ),
            ],
        )

        result = reporting_change_impact(self.conn, "provision:article-4")

        self.assertEqual(result["counts"]["direct_coordinates"], 1)
        self.assertEqual(result["counts"]["materialized_direct_cells"], 1)
        self.assertEqual(result["counts"]["instruction_defined_coordinates"], 0)
        coordinate = result["returns"][0]["direct_coordinates"][0]
        self.assertEqual(coordinate["impact_tier"], "direct_coordinate_evidence")
        self.assertEqual(coordinate["datapoint_id"], "datapoint:PRA115:r010:c020")
        self.assertEqual(coordinate["row_code"], "010")
        self.assertEqual(coordinate["column_code"], "020")
        self.assertIn("Review the passage", coordinate["review_note"])
        self.assertIn(
            "does not by itself prove",
            result["impact_model"]["direct_coordinate_evidence"],
        )


if __name__ == "__main__":
    unittest.main()
