import sqlite3
import unittest
from pathlib import Path

from backend.app.migrations import apply_migrations
from backend.app.reporting import (
    _dedupe_template_summaries,
    _template_identity_key,
    reporting_catalog,
    reporting_catalog_cells,
    reporting_catalog_return,
)
from scripts.enrich_reporting_catalog import validate_return_description


ROOT = Path(__file__).resolve().parents[1]


class ReportingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript((ROOT / "schema.sql").read_text())
        apply_migrations(self.conn)
        self.conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,description,estate,family,source_page_url,status
            ) VALUES ('r1','PRA115','Step-in risk','Step-in risk assessment.',
                      'supervisory_reporting','PRA data items','https://example.test/catalog','current')
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_artifact(
              artifact_id,url,display_title,artifact_role,estate,file_type,sheet_names_json,
              classification_method,classification_confidence
            ) VALUES ('a1','https://example.test/pra115.xlsx','PRA115 (XLSX)','template',
                      'supervisory_reporting','xlsx','["PRA115","Definitions"]','official_table',1)
            """
        )
        self.conn.execute(
            "INSERT INTO reporting_return_artifact(return_id,artifact_id,relationship) VALUES ('r1','a1','template')"
        )
        self.conn.execute(
            "INSERT INTO reporting_regime(regime_id,name) VALUES ('regime:regulatory','Regulatory reporting')"
        )
        self.conn.execute(
            """
            INSERT INTO reporting_collection(collection_id,regime_id,name)
            VALUES ('collection:pra','regime:regulatory','PRA data items')
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_requirement(
              requirement_id,collection_id,requirement_type,code,name
            ) VALUES (
              'requirement:pra115','collection:pra','regulatory_return','PRA115','Step-in risk'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_requirement_edition(
              edition_id,requirement_id,official_name,legacy_return_id,status,source_page_url
            ) VALUES (
              'edition:pra115','requirement:pra115','Step-in risk','r1','current',
              'https://example.test/catalog'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk)
            VALUES
              ('data_item:PRA115','DataItem','PRA115','reporting_obligation','data_item:PRA115'),
              ('template:PRA115','Template','PRA115 template','template','template:PRA115'),
              ('datapoint:PRA115:r010:c020','DataPoint','Step-in exposure','datapoint','datapoint:PRA115:r010:c020')
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method
            ) VALUES
              ('uses-template','data_item:PRA115','template:PRA115','USES_TEMPLATE',1,'manifest'),
              ('has-cell','template:PRA115','datapoint:PRA115:r010:c020','HAS_DATAPOINT',1,'manifest')
            """
        )
        self.conn.execute(
            """
            INSERT INTO source_document(source_id,title,url,file_type)
            VALUES (
              'source:pra115','PRA115',
              'https://example.test/pra115.xlsx','xlsx'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO template(template_id,template_code,title,source_id)
            VALUES (
              'template:PRA115','PRA115','Step-in risk template','source:pra115'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO template_row(row_id,template_id,row_code,row_order,label)
            VALUES ('row:PRA115:010','template:PRA115','010',1,'Step-in exposure')
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
              datapoint_id,template_id,row_id,column_id,data_type,unit_type,concept_label
            ) VALUES (
              'datapoint:PRA115:r010:c020','template:PRA115',
              'row:PRA115:010','column:PRA115:020','monetary','GBP','Step-in exposure'
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_catalog_returns_human_facing_fields_and_counts(self):
        result = reporting_catalog(self.conn, estate="supervisory_reporting")
        self.assertEqual(result["counts"]["returns"], 1)
        self.assertEqual(result["returns"][0]["name"], "Step-in risk")
        self.assertEqual(result["returns"][0]["template_count"], 1)
        self.assertNotIn("updated_at", result["returns"][0])

    def test_catalog_detail_exposes_workbook_sheets_without_audit_fields(self):
        result = reporting_catalog_return(self.conn, "r1")
        self.assertEqual(result["artifacts"][0]["sheet_names"], ["PRA115", "Definitions"])
        self.assertNotIn("classification_method", result["artifacts"][0])
        self.assertEqual(result["rulebook_references"], [])

    def test_catalog_cells_bridge_edition_to_existing_cell_corpus(self):
        result = reporting_catalog_cells(self.conn, "edition:pra115", q="exposure")

        self.assertEqual(result["return"]["return_code"], "PRA115")
        self.assertEqual(result["coverage"], "available")
        self.assertEqual(result["counts"], {"templates": 1, "cells": 1, "matched_cells": 1})
        self.assertEqual(result["templates"][0]["template_code"], "PRA115")
        self.assertEqual(result["cells"][0]["row_code"], "010")
        self.assertEqual(result["cells"][0]["column_code"], "020")
        self.assertEqual(result["cells"][0]["unit_type"], "GBP")

    def test_catalog_cells_surfaces_unknown_template_filter_without_cross_return_leakage(self):
        result = reporting_catalog_cells(self.conn, "r1", template_id="template:elsewhere")

        self.assertIsNone(result["selected_template_id"])
        self.assertEqual(result["counts"]["matched_cells"], 1)

    def test_catalog_cells_recovers_missing_relational_coordinate_links_from_datapoint_id(self):
        self.conn.execute(
            """
            INSERT INTO template_row(row_id,template_id,row_code,row_order,label)
            VALUES ('row:PRA115:030','template:PRA115','030',2,'Recovered row')
            """
        )
        self.conn.execute(
            """
            INSERT INTO datapoint(
              datapoint_id,template_id,row_id,column_id,data_type,concept_label
            ) VALUES (
              'datapoint:template:PRA115:r030:c020','template:PRA115',
              NULL,'column:PRA115:020','monetary','Recovered amount'
            )
            """
        )

        result = reporting_catalog_cells(
            self.conn,
            "r1",
            template_id="template:PRA115",
            limit=10,
        )
        recovered = next(
            cell for cell in result["cells"]
            if cell["datapoint_id"] == "datapoint:template:PRA115:r030:c020"
        )

        self.assertEqual(recovered["row_code"], "030")
        self.assertEqual(recovered["row_label"], "Recovered row")
        self.assertEqual(recovered["row_order"], 2)
        self.assertEqual(recovered["column_code"], "020")
        self.assertEqual(recovered["column_label"], "Amount")

    def test_catalog_cells_maps_annex_edition_by_exact_official_template_source(self):
        self.conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,estate,family,source_page_url,status
            ) VALUES (
              'r2','CRR-ANNEXES-I-II','Own funds',
              'supervisory_reporting','CRR supervisory reporting',
              'https://example.test/catalog','current'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_artifact(
              artifact_id,url,display_title,artifact_role,estate,file_type,
              classification_method,classification_confidence
            ) VALUES (
              'a2','https://example.test/annex-i.xlsx','Annex I (XLSX)',
              'template','supervisory_reporting','xlsx','official_table',1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_return_artifact(
              return_id,artifact_id,relationship
            ) VALUES ('r2','a2','template')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO source_document(source_id,title,url,file_type)
            VALUES (?,?,?,'xlsx')
            """,
            [
                ("annex-source", "Annex I", "https://example.test/annex-i.xlsx"),
                ("other-source", "Other", "https://example.test/other.xlsx"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO graph_node(
              node_id,node_type,label,source_table,source_pk
            ) VALUES (?,?,?,?,?)
            """,
            [
                (
                    "data_item:COREP-OWN-FUNDS",
                    "DataItem",
                    "COREP own funds",
                    "reporting_obligation",
                    "data_item:COREP-OWN-FUNDS",
                ),
                (
                    "source_document:annex-source",
                    "SourceDocument",
                    "Annex I",
                    "source_document",
                    "annex-source",
                ),
                (
                    "template:annex",
                    "Template",
                    "Annex template",
                    "template",
                    "template:annex",
                ),
                (
                    "template:other",
                    "Template",
                    "Unrelated template",
                    "template",
                    "template:other",
                ),
                (
                    "datapoint:annex",
                    "DataPoint",
                    "Own funds amount",
                    "datapoint",
                    "datapoint:annex",
                ),
                (
                    "datapoint:other",
                    "DataPoint",
                    "Unrelated amount",
                    "datapoint",
                    "datapoint:other",
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,confidence,
              extraction_method
            ) VALUES (?,?,?,?,1,'test')
            """,
            [
                (
                    "corep-source",
                    "data_item:COREP-OWN-FUNDS",
                    "source_document:annex-source",
                    "EVIDENCED_BY",
                ),
                (
                    "corep-annex",
                    "data_item:COREP-OWN-FUNDS",
                    "template:annex",
                    "USES_TEMPLATE",
                ),
                (
                    "corep-other",
                    "data_item:COREP-OWN-FUNDS",
                    "template:other",
                    "USES_TEMPLATE",
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO template(template_id,template_code,title,source_id)
            VALUES (?,?,?,?)
            """,
            [
                ("template:annex", "C01.00", "Own funds", "annex-source"),
                ("template:other", "C99.00", "Other", "other-source"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO template_row(
              row_id,template_id,row_code,row_order,label
            ) VALUES (?,?,?,1,?)
            """,
            [
                ("row:annex", "template:annex", "010", "Own funds"),
                ("row:other", "template:other", "999", "Other"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO template_column(
              column_id,template_id,column_code,column_order,label
            ) VALUES (?,?,?,1,'Amount')
            """,
            [
                ("column:annex", "template:annex", "020"),
                ("column:other", "template:other", "999"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO datapoint(
              datapoint_id,template_id,row_id,column_id,concept_label
            ) VALUES (?,?,?,?,?)
            """,
            [
                (
                    "datapoint:annex",
                    "template:annex",
                    "row:annex",
                    "column:annex",
                    "Own funds amount",
                ),
                (
                    "datapoint:other",
                    "template:other",
                    "row:other",
                    "column:other",
                    "Unrelated amount",
                ),
            ],
        )

        result = reporting_catalog_cells(self.conn, "r2")

        self.assertEqual(result["coverage"], "available")
        self.assertEqual(
            result["return"]["data_item_ids"],
            ["data_item:COREP-OWN-FUNDS"],
        )
        self.assertEqual(
            result["return"]["cell_mapping_basis"],
            "official_template_source",
        )
        self.assertEqual(result["counts"], {
            "templates": 1,
            "cells": 1,
            "matched_cells": 1,
        })
        self.assertEqual(result["templates"][0]["template_id"], "template:annex")
        self.assertEqual(result["cells"][0]["concept_label"], "Own funds amount")

    def test_artifact_route_does_not_leak_other_templates_from_aggregate_data_item(self):
        self.conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,estate,family,source_page_url,status
            ) VALUES (
              'r3','DISCLOSURE-ANNEXES-I-II','Disclosure',
              'pillar3_disclosure','Pillar 3 disclosures',
              'https://example.test/catalog','current'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_artifact(
              artifact_id,url,display_title,artifact_role,estate,file_type,
              classification_method,classification_confidence
            ) VALUES (
              'a3','https://example.test/disclosure.xlsx',
              'Disclosure Annex I (XLSX)','template',
              'pillar3_disclosure','xlsx','official_table',1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_return_artifact(
              return_id,artifact_id,relationship
            ) VALUES ('r3','a3','template')
            """
        )
        self.conn.execute(
            """
            INSERT INTO source_document(source_id,title,url,file_type)
            VALUES (
              'disclosure-source','Disclosure Annex I',
              'https://example.test/disclosure.xlsx','xlsx'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_node(
              node_id,node_type,label,source_table,source_pk
            ) VALUES (
              'source_document:disclosure-source','SourceDocument',
              'Disclosure Annex I','source_document','disclosure-source'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,confidence,
              extraction_method
            ) VALUES (
              'aggregate-disclosure-source','data_item:PRA115',
              'source_document:disclosure-source','EVIDENCED_BY',1,'test'
            )
            """
        )

        result = reporting_catalog_cells(self.conn, "r3")

        self.assertEqual(result["coverage"], "return_not_mapped")
        self.assertEqual(result["counts"], {
            "templates": 0,
            "cells": 0,
            "matched_cells": 0,
        })
        self.assertEqual(
            result["return"]["data_item_ids"],
            ["data_item:PRA115"],
        )

    def test_catalog_cells_reads_graph_native_template_and_datapoint_corpus(self):
        self.conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,estate,family,source_page_url,status
            ) VALUES (
              'r4','CRR-ANNEXES-III-V','Financial information',
              'supervisory_reporting','CRR supervisory reporting',
              'https://example.test/catalog','current'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_artifact(
              artifact_id,url,display_title,artifact_role,estate,file_type,
              classification_method,classification_confidence
            ) VALUES (
              'a4','https://example.test/finrep.xlsx','Annex III (XLSX)',
              'template','supervisory_reporting','xlsx','official_table',1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_return_artifact(
              return_id,artifact_id,relationship
            ) VALUES ('r4','a4','template')
            """
        )
        self.conn.execute(
            """
            INSERT INTO source_document(source_id,title,url,file_type)
            VALUES (
              'finrep-source','Annex III',
              'https://example.test/finrep.xlsx','xlsx'
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO graph_node(
              node_id,node_type,label,source_table,source_pk,properties_json
            ) VALUES (?,?,?,?,?,?)
            """,
            [
                (
                    "source_document:finrep-source",
                    "SourceDocument",
                    "Annex III",
                    "source_document",
                    "finrep-source",
                    "{}",
                ),
                (
                    "template:FINREP:FINREP_42_",
                    "Template",
                    "FINREP 42",
                    "template",
                    "template:FINREP:FINREP_42_",
                    '{"data_item_code":"FINREP","source_id":"finrep-source"}',
                ),
                (
                    "row:template:FINREP:FINREP_42_:0010",
                    "TemplateRow",
                    "FINREP row 0010 Property plant and equipment",
                    "template_row",
                    "row:template:FINREP:FINREP_42_:0010",
                    '{"template_id":"template:FINREP:FINREP_42_"}',
                ),
                (
                    "column:template:FINREP:FINREP_42_:0020",
                    "TemplateColumn",
                    "FINREP column 0020 Carrying amount",
                    "template_column",
                    "column:template:FINREP:FINREP_42_:0020",
                    '{"template_id":"template:FINREP:FINREP_42_"}',
                ),
                (
                    "datapoint:template:FINREP:FINREP_42_:r0010:c0020",
                    "DataPoint",
                    "Property plant and equipment",
                    "datapoint",
                    "datapoint:template:FINREP:FINREP_42_:r0010:c0020",
                    (
                        '{"template_id":"template:FINREP:FINREP_42_",'
                        '"row_code":"0010","column_code":"0020"}'
                    ),
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO graph_edge(
              edge_id,source_node_id,target_node_id,edge_type,confidence,
              extraction_method
            ) VALUES (?,?,?,?,1,'test')
            """,
            [
                (
                    "finrep-row",
                    "template:FINREP:FINREP_42_",
                    "row:template:FINREP:FINREP_42_:0010",
                    "HAS_ROW",
                ),
                (
                    "finrep-column",
                    "template:FINREP:FINREP_42_",
                    "column:template:FINREP:FINREP_42_:0020",
                    "HAS_COLUMN",
                ),
                (
                    "finrep-cell",
                    "template:FINREP:FINREP_42_",
                    "datapoint:template:FINREP:FINREP_42_:r0010:c0020",
                    "HAS_DATAPOINT",
                ),
                (
                    "finrep-cell-parallel-evidence",
                    "template:FINREP:FINREP_42_",
                    "datapoint:template:FINREP:FINREP_42_:r0010:c0020",
                    "HAS_DATAPOINT",
                ),
            ],
        )

        result = reporting_catalog_cells(self.conn, "r4", q="plant")

        self.assertEqual(result["coverage"], "available")
        self.assertEqual(result["counts"], {
            "templates": 1,
            "cells": 1,
            "matched_cells": 1,
        })
        self.assertEqual(result["templates"][0]["template_code"], "42")
        self.assertEqual(len(result["cells"]), 1)
        self.assertEqual(result["cells"][0]["template_code"], "42")
        self.assertEqual(result["cells"][0]["row_code"], "0010")
        self.assertEqual(result["cells"][0]["row_label"], "Property plant and equipment")
        self.assertEqual(result["cells"][0]["column_code"], "0020")
        self.assertEqual(result["cells"][0]["column_label"], "Carrying amount")

        unfiltered = reporting_catalog_cells(self.conn, "r4")
        self.assertEqual(unfiltered["counts"]["matched_cells"], 1)
        self.assertEqual(len(unfiltered["cells"]), 1)

    def test_template_identity_keeps_decimal_codes_distinct(self):
        self.assertNotEqual(
            _template_identity_key("1.1"),
            _template_identity_key("11"),
        )
        self.assertEqual(
            _template_identity_key(" C 01.00 "),
            _template_identity_key("C01.00"),
        )

    def test_duplicate_template_projection_prefers_edition_code(self):
        common = {
            "source_url": "https://example.test/pra110.xlsx",
            "cell_count": 10,
        }
        result = _dedupe_template_summaries(
            [
                {
                    **common,
                    "template_id": "template:COR011:COR011_PRA110_currency",
                    "template_code": "COR011",
                },
                {
                    **common,
                    "template_id": "template:PRA110:PRA110_PRA110_currency",
                    "template_code": "PRA110",
                },
            ],
            preferred_code="PRA110",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["template_code"], "PRA110")

    def test_enrichment_cannot_reclassify_supervisory_return_as_pillar_3(self):
        with self.assertRaisesRegex(ValueError, "supervisory return description"):
            validate_return_description(
                "CRR Pillar 3 disclosure for reporting on own funds.",
                expected_estate="supervisory_reporting",
            )

    def test_enrichment_accepts_estate_consistent_descriptions(self):
        self.assertEqual(
            validate_return_description(
                "CRR supervisory return for reporting on own funds.",
                expected_estate="supervisory_reporting",
            ),
            "CRR supervisory return for reporting on own funds.",
        )
        self.assertEqual(
            validate_return_description(
                "Pillar 3 disclosure of own funds.",
                expected_estate="pillar3_disclosure",
            ),
            "Pillar 3 disclosure of own funds.",
        )


if __name__ == "__main__":
    unittest.main()
