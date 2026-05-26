from pathlib import Path

from autocode.preprint_release import audit_pdf, facility_text, metadata_template, write_kit


def test_facility_text_includes_required_rrids():
    text = facility_text("ICP-MS")

    assert "ICP-MS was performed" in text["methods"]
    assert "QBIC" in text["methods"]
    assert "IMSERC" in text["methods"]
    assert "SCR_017773" in text["methods"]
    assert "SCR_017874" in text["acknowledgements"]


def test_metadata_template_uses_pseudonymous_creator():
    metadata = metadata_template(title="Example", pseudonym="SN Org", technique="ICP-MS")

    assert metadata["title"] == "Example"
    assert metadata["creators"] == [{"name": "SN Org", "affiliation": "Independent Researcher"}]
    assert metadata["resource_type"] == "preprint"
    assert metadata["facility_acknowledgement"]["methods"].startswith("ICP-MS was performed")
    assert any(note["platform"] == "Zenodo" for note in metadata["source_notes"])


def test_write_kit_creates_release_files(tmp_path: Path):
    paths = write_kit(tmp_path, title="Example Paper", pseudonym="Anonymous", technique="elemental analysis")

    checklist = Path(paths["checklist"]).read_text(encoding="utf-8")
    facility = Path(paths["facility_text"]).read_text(encoding="utf-8")
    metadata = Path(paths["metadata"]).read_text(encoding="utf-8")

    assert "Example Paper" in checklist
    assert "Anonymous" in checklist
    assert "Zenodo" in checklist
    assert "elemental analysis was performed" in facility
    assert '"name": "Anonymous"' in metadata


def test_pdf_audit_flags_identity_leaks_and_missing_ack(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n/Author (Luke Example)\nContact luke@example.com\n%%EOF")

    audit = audit_pdf(pdf)

    assert not audit.ok_to_review
    assert "found email address" in audit.findings
    assert "found PDF Author metadata" in audit.findings
    assert "missing IMSERC acknowledgement marker" in audit.findings
    assert "missing QBIC acknowledgement marker" in audit.findings


def test_pdf_audit_accepts_minimal_acknowledged_pdf(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"IMSERC RRID:SCR_017874\n"
        b"QBIC RRID:SCR_017773\n"
        b"%%EOF"
    )

    audit = audit_pdf(pdf)

    assert audit.ok_to_review
    assert audit.findings == ["found IMSERC/QBIC text"]
