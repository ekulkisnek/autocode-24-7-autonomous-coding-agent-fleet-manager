from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


FACILITY_METHODS = (
    "{technique} was performed at the Northwestern University Quantitative "
    "Bulk-Elemental Information Core (QBIC) (RRID:SCR_017773) within the "
    "Integrated Molecular Structure Education and Research Center (IMSERC) "
    "(RRID:SCR_017874)."
)

FACILITY_ACKNOWLEDGEMENT = (
    "This research utilized facilities at the Integrated Molecular Structure "
    "Education and Research Center (IMSERC) (RRID:SCR_017874) and the "
    "Quantitative Bulk-Elemental Information Core (QBIC) at Northwestern "
    "University."
)

SOURCE_NOTES = [
    {
        "platform": "Zenodo",
        "source": "https://about.zenodo.org/policies/",
        "note": "Zenodo says anyone may register and deposit research artifacts, files can be open/closed/embargoed/restricted, and a DOI is registered when a record is published.",
    },
    {
        "platform": "Zenodo",
        "source": "https://help.zenodo.org/docs/deposit/about-records/",
        "note": "Zenodo records require minimal citation metadata; files and persistent identifiers cannot be modified after publication.",
    },
    {
        "platform": "Figshare",
        "source": "https://info.figshare.com/user-guide/figshare-policies/",
        "note": "Figshare requires an account with a valid email address and supports DOI/citation metadata.",
    },
    {
        "platform": "OSF Preprints",
        "source": "https://help.osf.io/article/376-preprints-home-page",
        "note": "OSF community preprint services use moderation and pending/private submission states before acceptance.",
    },
]


@dataclass(frozen=True)
class PlatformStep:
    name: str
    purpose: str
    anonymity: str
    action: str
    caution: str


@dataclass(frozen=True)
class PdfAudit:
    path: str
    ok_to_review: bool
    findings: list[str]


PLATFORM_STEPS = [
    PlatformStep(
        "Zenodo",
        "Primary DOI home base",
        "Pseudonymous public metadata can be prepared, but the account itself is not anonymous.",
        "Create a draft record, use the pseudonym as creator metadata, upload the anonymized PDF, choose access state, then publish only after review.",
        "Do not publish until PDF metadata and acknowledgements are checked; after publication files and DOI cannot be edited in place.",
    ),
    PlatformStep(
        "Figshare",
        "Secondary DOI / private-link review option",
        "Public item metadata can be pseudonymous; account email exists behind the service.",
        "Create an item, add creator details for the pseudonym, reserve DOI/private link if needed, upload the same PDF, then publish after review.",
        "Validate license and item type; avoid adding ORCID or institutional identifiers.",
    ),
    PlatformStep(
        "OSF / OSF Preprints",
        "Project page and/or preprint submission",
        "Good for view-only links and moderated preprint workflows.",
        "Create a pseudonymous project/profile, upload the PDF, generate a view-only link, then submit to a suitable OSF preprint service if appropriate.",
        "Moderation can reject scope/content; public project metadata should be checked for account/profile leakage.",
    ),
    PlatformStep(
        "GitHub / static site",
        "Mirror and discoverability",
        "Depends entirely on account/domain hygiene.",
        "Publish PDF, metadata, and DOI links from the primary record.",
        "Repository ownership, commit metadata, DNS, analytics, and TLS certificates can identify you.",
    ),
    PlatformStep(
        "ResearchGate / Academia.edu / social forums",
        "Distribution after DOI exists",
        "Pseudonymous profile is possible but lower-confidence.",
        "Post the DOI link and the same PDF only after the primary DOI is live.",
        "These services may ask for affiliation/contact details and can expose profile/network clues.",
    ),
]


def facility_text(technique: str) -> dict[str, str]:
    clean = " ".join((technique or "Trace elemental analysis").split())
    return {
        "methods": FACILITY_METHODS.format(technique=clean),
        "acknowledgements": FACILITY_ACKNOWLEDGEMENT,
    }


def metadata_template(*, title: str, pseudonym: str, technique: str) -> dict:
    text = facility_text(technique)
    return {
        "title": title,
        "creators": [{"name": pseudonym, "affiliation": "Independent Researcher"}],
        "resource_type": "preprint",
        "license": "CC-BY-4.0",
        "description": "Anonymized preprint. Replace this with a concise abstract before upload.",
        "facility_acknowledgement": text,
        "keywords": ["preprint", "anonymous", "IMSERC", "QBIC"],
        "upload_sequence": [step.name for step in PLATFORM_STEPS],
        "source_notes": SOURCE_NOTES,
    }


def checklist_markdown(*, title: str, pseudonym: str, technique: str) -> str:
    text = facility_text(technique)
    lines = [
        f"# Anonymous Preprint Release Kit: {title}",
        "",
        "## Non-Negotiable Checks",
        "",
        "- Use the same reviewed PDF for every platform.",
        "- Keep the IMSERC/QBIC facility text in Methods and Acknowledgements.",
        "- Do not publish until PDF metadata, author names, self-citations, paths, images, and supplemental files have been checked.",
        "- Treat platform account identity as non-anonymous even when public metadata is pseudonymous.",
        "",
        "## Facility Text",
        "",
        "Methods:",
        "",
        text["methods"],
        "",
        "Acknowledgements:",
        "",
        text["acknowledgements"],
        "",
        "## Platform Order",
        "",
    ]
    for i, step in enumerate(PLATFORM_STEPS, start=1):
        lines.extend(
            [
                f"{i}. {step.name}",
                f"   - Purpose: {step.purpose}",
                f"   - Public identity: {pseudonym}",
                f"   - Action: {step.action}",
                f"   - Caution: {step.caution}",
                "",
            ]
        )
    lines.extend(
        [
            "## Manual Review Before Upload",
            "",
            "- Search the PDF for real names, initials, emails, phone numbers, addresses, lab names, funding numbers, file paths, usernames, ORCID, and acknowledgements beyond IMSERC/QBIC.",
            "- Check document properties in Preview/Acrobat or with `pdfinfo` if installed.",
            "- Re-export the PDF from a clean document if author metadata is embedded.",
            "- Upload first as a draft/private/restricted item where the platform supports it.",
            "- Record the DOI/private link here before cross-posting.",
            "",
            "## Source Notes Checked",
            "",
        ]
    )
    for source in SOURCE_NOTES:
        lines.append(f"- {source['platform']}: {source['note']} ({source['source']})")
    lines.append("")
    return "\n".join(lines)


def _decode_pdf_bytes(path: Path) -> str:
    data = path.read_bytes()
    return data.decode("latin-1", errors="ignore")


def audit_pdf(path: Path, *, allowed_terms: Iterable[str] = ()) -> PdfAudit:
    findings: list[str] = []
    if not path.exists():
        return PdfAudit(str(path), False, [f"missing file: {path}"])
    if path.suffix.lower() != ".pdf":
        findings.append("file does not have a .pdf extension")

    text = _decode_pdf_bytes(path)
    allowed = {term.lower() for term in allowed_terms}
    patterns = {
        "email address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "ORCID": r"\borcid\b|0000-\d{4}-\d{4}-\d{3}[\dX]",
        "PDF Author metadata": r"/Author\s*\(",
        "PDF Creator metadata": r"/Creator\s*\(",
        "PDF Producer metadata": r"/Producer\s*\(",
        "local file path": r"/Users/[A-Za-z0-9._-]+|/Volumes/[A-Za-z0-9._-]+|C:\\\\Users\\\\",
        "Northwestern outside required acknowledgement": r"Northwestern",
        "IMSERC/QBIC text": r"IMSERC|QBIC|SCR_017773|SCR_017874",
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text, re.I) and label.lower() not in allowed:
            findings.append(f"found {label}")

    has_imserc = re.search(r"IMSERC|SCR_017874", text, re.I)
    has_qbic = re.search(r"QBIC|SCR_017773", text, re.I)
    if not has_imserc:
        findings.append("missing IMSERC acknowledgement marker")
    if not has_qbic:
        findings.append("missing QBIC acknowledgement marker")

    blocking = [f for f in findings if not f.startswith("found IMSERC/QBIC")]
    return PdfAudit(str(path), not blocking, findings)


def write_kit(output_dir: Path, *, title: str, pseudonym: str, technique: str, pdf: Path | None = None) -> dict[str, Path | PdfAudit]:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = metadata_template(title=title, pseudonym=pseudonym, technique=technique)
    paths: dict[str, Path | PdfAudit] = {}

    metadata_path = output_dir / "metadata-template.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["metadata"] = metadata_path

    checklist_path = output_dir / "release-checklist.md"
    checklist_path.write_text(checklist_markdown(title=title, pseudonym=pseudonym, technique=technique), encoding="utf-8")
    paths["checklist"] = checklist_path

    ack_path = output_dir / "facility-text.txt"
    text = facility_text(technique)
    ack_path.write_text(f"Methods:\n{text['methods']}\n\nAcknowledgements:\n{text['acknowledgements']}\n", encoding="utf-8")
    paths["facility_text"] = ack_path

    if pdf is not None:
        audit = audit_pdf(pdf)
        audit_path = output_dir / "pdf-audit.json"
        audit_path.write_text(json.dumps(asdict(audit), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths["pdf_audit"] = audit_path
        paths["pdf_audit_result"] = audit
    return paths
