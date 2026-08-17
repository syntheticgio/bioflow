"""Reading a protein FASTA header.

No network, no database: this module is string parsing, and the tests are
the specification of which header shapes the viewer can resolve.

The regexes are deliberately strict, following the reasoning recorded in
`metadata/uniprot.py`: a loose accession pattern classifies the gene symbol
EGFR as an accession and returns nothing useful.
"""

import pytest
from app.metadata.protein_headers import RefKind, parse_header


@pytest.mark.parametrize(
    "header,accession",
    [
        # UniProt's own FASTA format, the shape a proteome download has.
        (">sp|P00924|ENO1_YEAST Enolase 1 OS=Saccharomyces cerevisiae", "P00924"),
        # TrEMBL uses tr| rather than sp|; both are UniProtKB accessions.
        (">tr|A0A0B7P3V8|A0A0B7P3V8_YEAST Uncharacterized protein", "A0A0B7P3V8"),
        # The leading '>' is optional -- callers may have stripped it.
        ("sp|P00924|ENO1_YEAST Enolase 1", "P00924"),
    ],
)
def test_uniprot_headers(header, accession):
    ref = parse_header(header)
    assert ref is not None
    assert ref.kind is RefKind.UNIPROT
    assert ref.accession == accession


@pytest.mark.parametrize(
    "header,accession",
    [
        # NCBI RefSeq protein, the shape `protein.faa` from a genome download has.
        (">NP_009342.1 Cdc19p [Saccharomyces cerevisiae S288C]", "NP_009342"),
        (">XP_011542244.1 pyruvate kinase isoform X1 [Homo sapiens]", "XP_011542244"),
        # WP_ is the non-redundant bacterial protein prefix.
        (">WP_000177921.1 chaperonin GroEL [Escherichia coli]", "WP_000177921"),
        # An unversioned accession is still an accession.
        (">NP_009342 Cdc19p", "NP_009342"),
    ],
)
def test_refseq_headers(header, accession):
    """The version suffix is stripped.

    UniProt's cross-reference index is keyed on the unversioned accession:
    `xref:refseq-NP_009342` matches where `xref:refseq-NP_009342.1` does not.
    """
    ref = parse_header(header)
    assert ref is not None
    assert ref.kind is RefKind.REFSEQ
    assert ref.accession == accession


@pytest.mark.parametrize(
    "header",
    [
        # Prokka/Bakta annotation output: a locus tag, not a database ID.
        ">KLLIPMDF_00023 hypothetical protein",
        # A bare gene symbol is not an accession. This is the case the strict
        # regex exists for: a loose pattern would classify EGFR as one.
        ">EGFR",
        # An assembly accession is not a protein accession.
        ">GCF_000002445.2 something",
        # Degenerate inputs are ordinary misses, not errors.
        ">",
        "",
        "   ",
    ],
)
def test_unrecognized_headers_return_none(header):
    """Naming no identifier is an ordinary outcome, not an error (R12)."""
    assert parse_header(header) is None
