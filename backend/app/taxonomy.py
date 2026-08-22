"""Central registry of allowed graph taxonomy values.

Every node_type, edge_type, source_method and provenance class used by the
explorer must be declared here. Adding a value is a deliberate code change
(reviewable in git) rather than something that can emerge silently from a
scraper, LLM pass or manual script.
"""
from __future__ import annotations

from typing import FrozenSet

NODE_TYPES: FrozenSet[str] = frozenset({
    "chapter", "crr_terms_list", "defined_term", "external_reference",
    "glossary", "guidance_document", "guidance_index", "guidance_paragraph",
    "guidance_section", "legal_instrument", "legal_instruments_index",
    "obligation_pattern", "obligation_statement", "part", "permission",
    "provision", "rule", "rule_reference", "rulebook",
})

EDGE_TYPES: FrozenSet[str] = frozenset({
    "amends", "contains", "defines", "has_obligation_pattern",
    "has_permission", "has_structured_obligation", "has_version",
    "references", "shares_defined_term", "shares_obligation_pattern",
    "sourced_from", "uses_defined_term",
})

SOURCE_METHODS: FrozenSet[str] = frozenset({
    "crr_terms_source", "derived_obligation_overlap", "derived_term_overlap",
    "fca_waivers_list", "glossary_source", "html_anchor_resolved",
    "html_anchor_unresolved", "html_glossary_link", "html_link",
    "inline_part_definition", "legal_identity", "legal_instrument_listing",
    "legal_reference_occurrence_v1", "llm_extracted_reference",
    "pdf_text_extraction", "reference_recall_stage_v1",
    "regex_article_reference", "regex_internal_article_reference_v2",
    "regex_named_reference", "regex_obligation", "regex_reference",
    "regex_uk_crr_article_reference_v2", "resolution_policy_v1",
    "resolved_part_reference", "rollup_child_edge",
    "rollup_resolved_part_reference", "site_structure",
    "structured_obligation_parser", "uk_crr_reference_repair",
})

# Provenance classes describe how confidently an assertion is grounded.
PROVENANCE_CLASSES: FrozenSet[str] = frozenset({
    "direct_text",
    "html_structure",
    "document_metadata",
    "inferred",
})

DERIVED_LAYERS: dict[str, FrozenSet[str]] = {
    "regex_reference": frozenset({
        "regex_reference", "regex_named_reference", "regex_article_reference",
        "regex_obligation", "regex_internal_article_reference_v2",
        "regex_uk_crr_article_reference_v2",
    }),
    "llm_extracted_reference": frozenset({"llm_extracted_reference"}),
    "rollup_child_edge": frozenset({"rollup_child_edge"}),
    "derived_overlap": frozenset({"derived_term_overlap", "derived_obligation_overlap"}),
}


def unknown_node_types(seen: set[str]) -> set[str]:
    return seen - NODE_TYPES


def unknown_edge_types(seen: set[str]) -> set[str]:
    return seen - EDGE_TYPES


def unknown_source_methods(seen: set[str]) -> set[str]:
    return seen - SOURCE_METHODS


def unknown_provenance_classes(seen: set[str]) -> set[str]:
    return seen - PROVENANCE_CLASSES
