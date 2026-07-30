# PRA Reporting Estate Ontology

This document defines the common hierarchy and nomenclature for the reporting
database, graph, API and user interface. It is called an ontology here to avoid
confusion with an XBRL taxonomy.

## Core hierarchy

| Level | Entity | Meaning | Example |
|---|---|---|---|
| 0 | Reporting estate | Everything covered by the explorer | PRA reporting estate |
| 1 | Regime | Fundamental regulatory purpose | Regulatory reporting; Pillar 3 disclosure |
| 2 | Collection | Official administrative family | PRA data items; FSA returns; CRR supervisory reporting |
| 3 | Reporting requirement | Stable requirement independent of version | PRA115 — Step-in risk |
| 4 | Requirement edition | Time-bounded version of the requirement | PRA115 effective 1 January 2026 |
| 5 | Resource | Official file implementing or explaining an edition | Workbook, instructions PDF, taxonomy package |
| 6 | Resource component | Meaningful structure inside a resource | Worksheet, template, instruction section, entry point |
| 7 | Data definition | Lowest reporting structure | Row, column, field, concept or validation rule |

The principal navigation hierarchy is:

```text
Reporting estate
└── Regime
    └── Collection
        └── Reporting requirement
            └── Requirement edition
                └── Resources
```

## Regimes

`Return` must not be used as the generic name for everything in the reporting
estate. Use three top-level classifications:

1. **Regulatory reporting** — information submitted to the PRA for supervisory
   purposes.
2. **Pillar 3 disclosure** — information published under disclosure
   requirements. These are not COREP returns.
3. **Shared reporting infrastructure** — XBRL taxonomies, DPM packages,
   validations, sample instances, filing rules and utilities.

Shared infrastructure supports reporting requirements but is not itself a
reporting obligation.

## Collections

Collections reflect official administrative families:

- CRR supervisory reporting
- PRA data items
- FSA returns
- Ring-fencing returns
- Mortgage reporting
- Pillar 3 disclosures
- Other PRA reporting
- Shared XBRL infrastructure

Subject areas such as liquidity, leverage, capital and market risk are tags,
not collections. A subject tag must never determine whether something is a
COREP return or a Pillar 3 disclosure.

## Reporting requirements

Use the generic class `ReportingRequirement`, with two principal subtypes:

```text
ReportingRequirement
├── RegulatoryReturn
└── DisclosureRequirement
```

Examples:

```text
RegulatoryReturn: PRA115 — Step-in risk
RegulatoryReturn: LVR002 — Contingent leverage
DisclosureRequirement: Disclosure of the leverage ratio
```

Naming rules:

- Reserve `return` for supervisory submissions.
- Retain `data item` only where it is the PRA's official terminology.
- Do not use `DataItem` as the generic graph root.
- Do not manufacture COREP return codes for Annex-based disclosures.
- A disclosure requirement can be identified by its official name and Annex
  pair.

## Requirement editions

The stable requirement must be separated from its published versions:

```text
PRA114 — Capital+ forecast annual
├── Edition effective until 31 December 2026
└── Edition effective from 1 January 2027
```

Edition properties include:

- effective-from and effective-to dates;
- status: future, current or superseded;
- official name and publication source;
- submission channel, where evidenced; and
- frequency, scope and units only where evidenced.

Templates, instructions and taxonomy entry points attach to an edition, not
directly to the timeless requirement.

In user-facing graphs, both the stable requirement and its editions use the
requirement's code and official name. Lifecycle status and effective dates are
secondary metadata shown by the inspector or status treatment; they are not
substitutes for the node's meaningful requirement label.

Structural hierarchy is visible in the reporting-estate overview but is hidden
by default after a requirement is selected. The requirement canvas prioritises
forms, guidance and related rules. When the catalogue contains multiple
editions of the same requirement, the UI presents them through a compact
edition/history control rather than adding requirement and edition wrapper
nodes to the default canvas.

## Resources

A `Resource` is an official published object. Resource role and file format are
separate properties.

### Resource roles

- Reporting template
- Reporting instructions
- Disclosure template
- Disclosure instructions
- XBRL taxonomy package
- DPM package
- Validation rules
- Sample instances
- Filing manual
- Release note
- Reporting schedule
- Utility
- Guidance

### Resource formats

- XLS/XLSX/XLTX
- PDF
- ZIP
- XML/XSD/XBRL
- HTML
- CSV

Official context determines the resource role. The extension determines only
the format. A PDF can therefore be a reporting template and a ZIP can contain
instructions.

`Artifact` may remain an internal engineering term, but the UI should use
`resource` or the specific resource role.

## Template components

```text
Template resource
└── Workbook or form
    ├── Worksheet
    │   └── Logical template/table
    │       ├── Section
    │       ├── Row
    │       ├── Column
    │       └── Field/cell
    └── Supporting worksheet
```

Important distinctions:

- A workbook is a file.
- A worksheet is a physical tab.
- A template is a logical reporting form or table.
- A worksheet and a template are not necessarily the same thing.
- Cover, index, definition and dropdown sheets are supporting worksheets, not
  reporting templates.

## Instruction components

```text
Instruction resource
└── Instruction section
    └── Instruction provision
```

An instruction provision can apply to a whole requirement, an edition, a
template, a row, a column, or a field or concept.

The deterministic instruction projection uses the following evidence path:

```text
Rulebook provision
↑ REFERENCES_RULE
InstructionProvision
├── EVIDENCED_BY → official instruction resource
├── APPLIES_TO → default template
└── INSTRUCTS → DataPoint, TemplateRow or ReportingCoordinate
```

`ReportingCoordinate` represents an explicit template/row/column combination
from an instruction where the template dimensions exist but the current
workbook parser did not create a `DataPoint`. Its
`coverage_status=instruction_defined_not_materialized` is a data-coverage fact,
not a weaker form of instruction evidence.

## Cell read model

The normalized catalogue and the existing parsed datapoint corpus are at
different lifecycle stages. Public cell navigation therefore uses an explicit
read-model bridge:

```text
Requirement edition
└── exact official-template source, or direct code when no artifact exists
    └── legacy DataItem
        └── USES_TEMPLATE
            └── Template (relational or graph-native)
                ├── TemplateRow
                ├── TemplateColumn
                └── DataPoint
```

This bridge is exposed by `GET /reporting/catalog/{return_id}/cells`. It is a
compatibility read model, not a new ontology edge. Consumers must inspect the
returned `coverage` value. Relational templates are preferred when the same
logical template code exists in both corpora; otherwise exact-source
graph-native templates and datapoints are included. Parallel graph edges to
the same datapoint do not create duplicate public cells, and duplicate
DataItem projections of the same workbook sheet are collapsed in favour of
the edition's own return code.

- `available` means parsed cells can be searched and paged;
- `templates_without_cells` means templates are linked but their cells have
  not been parsed into the current database;
- `return_not_mapped` means the catalogue edition has no link to a parsed
  legacy template.

The UI must surface these states directly. Absence of cells must never be
presented as evidence that a reporting requirement has no fields.

The cell viewer selects one logical template at a time and reconstructs its
row-by-column matrix from `TemplateRow`, `TemplateColumn` and `DataPoint`
coordinates. It retrieves every paged datapoint for that template before
rendering, preserves empty intersections, and keeps row and column headings
fixed while the matrix scrolls. Search narrows matching rows while retaining
the template's full ordered column structure. A flat cross-template cell
ledger is not the primary presentation.

When a normalized Annex code differs from the older aggregate data-item code,
the bridge may use an exact official template URL shared by the catalogue
artifact, source document and DataItem evidence. Templates are then constrained
to that same artifact URL. A source-level match must never expose every template
from an aggregate DataItem such as FINREP or COR011.

## Change-impact evidence tiers

Change-impact applications use `GET /reporting/impact/{target_node_id}` and
must preserve three evidence tiers:

1. `direct_instruction_reference` — an instruction source has an extracted,
   source-backed graph relationship to the changed rule or provision.
2. `direct_coordinate_evidence` — the same instruction provision expressly
   names the changed rule and a reporting row, column or cell. This narrows the
   review to exact coordinates but does not prove that an edit is required.
3. `candidate_scope` — templates and cells sit downstream of the affected
   reporting return and therefore require review, but no precise instruction
   provision has yet proved that each coordinate changes.

Applications must not relabel candidate scope as confirmed impact. A confirmed
cell edit requires legal and reporting review of the changed rule and its
instruction passage. An `INSTRUCTS` relationship proves that the passage names
the coordinate; it does not predict the semantic effect of the future rule
text.

## XBRL components

```text
XBRL taxonomy release
├── Taxonomy package
├── Module or entry point
├── Table definition
├── Concept
├── Dimension/domain/member
├── Validation rule
└── Sample instance
```

A taxonomy release such as Banking Taxonomy v4.1.0 is not a return. Its entry
points encode particular reporting requirements or editions.

## Graph relationships

| Relationship | Meaning |
|---|---|
| `HAS_REGIME` | Reporting estate contains a regime |
| `HAS_COLLECTION` | Regime contains an official collection |
| `BELONGS_TO_REGIME` | Requirement belongs to regulatory reporting or disclosure |
| `BELONGS_TO_COLLECTION` | Requirement belongs to an official collection |
| `HAS_EDITION` | Stable requirement has a dated edition |
| `SUPERSEDES` | One edition replaces another |
| `HAS_TEMPLATE_RESOURCE` | Edition uses an official template file |
| `HAS_INSTRUCTION_RESOURCE` | Edition uses an official instruction file |
| `CONTAINS_SHEET` | Workbook contains a worksheet |
| `IMPLEMENTS_TEMPLATE` | Resource or sheet implements a logical template |
| `CONTAINS_INSTRUCTION_SECTION` | Instruction resource contains an instruction section |
| `HAS_ROW` / `HAS_COLUMN` | Template contains reporting coordinates |
| `SUPPORTED_BY_TAXONOMY` | Edition uses an XBRL taxonomy release |
| `HAS_ENTRY_POINT` | Taxonomy release contains an entry point |
| `ENCODES_REQUIREMENT` | Entry point encodes a reporting requirement |
| `ENCODES_CONCEPT` | Field or coordinate maps to an XBRL concept |
| `HAS_VALIDATION_RULE` | Edition or taxonomy has a validation |
| `REQUIRED_BY_RULE` | Rulebook provision establishes or supports the requirement |
| `REFERENCES_RULE` | Instructions expressly reference a Rulebook provision |
| `INSTRUCTS` | Instruction provision explains a reporting component |
| `APPLIES_TO` | Authoritatively evidenced scope relationship |
| `EVIDENCED_BY` | Provenance only |

`EVIDENCED_BY` remains useful for provenance but must not replace the more
precise semantic relationships above.

## Human-readable name inheritance

Every ontology level supports an optional `display_name`. Names resolve
contextually through the hierarchy:

```text
resource/component override
→ edition override
→ requirement override
→ requirement code and official name
→ collection name
→ regime name
```

An associated resource inherits the requirement name and appends its role when
it has no override. For example:

```text
PRA115 — Step-in risk — Reporting template
PRA115 — Step-in risk — Reporting instructions
```

The official filename remains a separate property and is never overwritten by
the inherited display name. APIs expose both the resolved display name and its
source (`resource_override`, `edition_override`, or
`inherited_from_requirement`) so inheritance is transparent. Shared resources
resolve their names in the context of each associated edition.

## PRA115 example

```text
PRA reporting estate
└── Regulatory reporting
    └── PRA data items
        └── PRA115 — Step-in risk
            └── Edition effective 1 January 2026
                ├── Reporting template resource
                │   └── PRA115.xlsx
                │       ├── Cover [supporting worksheet]
                │       ├── Index [supporting worksheet]
                │       ├── SI 700.00 [reporting template]
                │       ├── SI 00.01 [reporting template]
                │       └── SI 00.02 [reporting template]
                ├── Reporting instructions resource
                │   └── PRA115 Instructions.pdf
                ├── Supported by taxonomy
                │   └── Banking Taxonomy release
                │       └── PRA115 entry point
                └── Rulebook relationships
                    ├── Step-in Risk Part
                    └── Regulatory Reporting Part
```

## Mapping from the current model

| Current term | Recommended term |
|---|---|
| `estate` | `regime` |
| `family` | `collection` |
| `reporting_return_catalog` row | `RequirementEdition` |
| `DataItem` graph root | `ReportingRequirement` or compatibility alias |
| `ReportingReturn` | `RegulatoryReturnEdition` |
| `DisclosureSet` | `DisclosureEdition` |
| `reporting_artifact` | `Resource` |
| `artifact_role` | `resource_role` |
| `SourceDocument` | `ResourceFile` |
| `TemplateSet` | `TemplateResource` or `TemplatePackage` |
| `InstructionSet` | `InstructionResource` |
| `Template` | Logical reporting template/table |
| `sheet_names_json` | Individual `Worksheet` nodes |
| `DataPoint` | `ReportableDataPoint` |

The most important structural change is splitting each current catalogue entry
into a stable reporting requirement and a dated requirement edition. This
vocabulary should be applied consistently across the database, graph, API and
UI.
