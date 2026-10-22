"""Get the text to extract from: posted registry results, PMC full text, an open-access PDF, or the abstract."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader

from .jats import jats_to_text
from .sources import Sources, europepmc_url, trial_results_text

Kind = Literal["registry", "pmc_xml", "oa_pdf", "abstract"]


@dataclass
class FullText:
    kind: Kind
    text: str  # what quotes are checked against; for a PDF, its text layer
    url: str
    pdf: bytes | None = None

    def document_block(self, title: str) -> dict:
        if self.pdf is not None:
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(self.pdf).decode()},
                "title": title[:500] or "Document",
            }
        return {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": self.text},
            "title": title[:500] or "Document",
        }


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def get_fulltext(paper: dict, sources: Sources) -> FullText | None:
    """Best available text for a paper row (see store.py), or None."""
    if paper["source"] == "clinicaltrials":
        study = sources.trials.study(paper["ext_id"])
        if study:
            return FullText(
                kind="registry",
                text=trial_results_text(study),
                url=f"https://clinicaltrials.gov/study/{paper['ext_id']}",
            )
        return None

    if paper.get("pmcid"):
        xml = sources.europepmc.fulltext_xml(paper["pmcid"])
        if xml:
            text = jats_to_text(xml)
            # Some records hold only front matter; those fall through to the next source.
            if len(text) > 500:
                return FullText(kind="pmc_xml", text=text, url=f"https://pmc.ncbi.nlm.nih.gov/articles/{paper['pmcid']}/")

    if paper.get("doi"):
        url = sources.unpaywall.pdf_url(paper["doi"])
        if url:
            data = sources.unpaywall.download_pdf(url)
            if data:
                return FullText(kind="oa_pdf", text=pdf_text(data), url=url, pdf=data)

    if paper.get("abstract"):
        text = f"# {paper['title']}\n\n## Abstract\n{paper['abstract']}\n"
        url = europepmc_url(paper["ext_id"]) if paper["source"] == "europepmc" else f"https://pubmed.ncbi.nlm.nih.gov/{paper['ext_id']}/"
        return FullText(kind="abstract", text=text, url=url)
    return None
