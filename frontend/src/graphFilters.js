const INSURANCE_PART_RE = /insurance|insurer|solvency ii|sii|non-solvency|policyholder|with-profits|actuar|matching adjustment|technical provisions|own funds/i;

export function isInsuranceNode(node) {
  const partContext = [
    node?.metadata?.part_title,
    node?.metadata?.document_title,
    ['part', 'chapter', 'guidance_document', 'guidance_section'].includes(node?.node_type) ? node?.title : '',
  ].filter(Boolean).join(' ');
  return INSURANCE_PART_RE.test(partContext);
}
