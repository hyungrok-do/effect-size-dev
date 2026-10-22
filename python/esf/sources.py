"""Clients for the literature sources: PubMed, ClinicalTrials.gov, Europe PMC and Unpaywall."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from defusedxml.ElementTree import fromstring

from . import __version__
from .config import Settings

# Base64 adds a third, and a request to the Claude API may be at most 32 MB.
MAX_PDF_BYTES = 20 * 1024 * 1024


@dataclass
class Record:
    # europepmc: an article in Europe PMC but not in PubMed (only in PubMed Central, or a preprint)
    source: Literal["pubmed", "clinicaltrials", "europepmc"]
    ext_id: str
    title: str
    authors: str = ""  # the first three, then "et al."
    year: str = ""
    journal: str = ""
    pubtypes: list[str] = field(default_factory=list)
    abstract: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    nct_ids: list[str] = field(default_factory=list)
    # Every author with their affiliations (see clean_affiliations), and for a registry record the
    # officials with their roles: {"name", "affiliations", "role"?}. Shown in the review's author table.
    author_list: list[dict] = field(default_factory=list)


class SourceError(RuntimeError):
    pass


class Http:
    """A shared HTTP client with retries on rate limits and server errors."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        contact = f" (mailto:{settings.contact_email})" if settings.contact_email else ""
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": f"effect-size-finder/{__version__}{contact}"},
        )

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        delay = 1.0
        for attempt in range(4):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise SourceError(f"{url}: {exc}") from exc
            else:
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                    return response
            time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")


def _year(text: str | None) -> str:
    text = text or ""
    for i in range(len(text) - 3):
        chunk = text[i : i + 4]
        if chunk.isdigit() and chunk[0] in "12":
            return chunk
    return ""


class PubMed:
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.common = {"tool": "effect-size-finder"}
        if settings.contact_email:
            self.common["email"] = settings.contact_email
        if settings.ncbi_api_key:
            self.common["api_key"] = settings.ncbi_api_key
        # NCBI allows 3 requests a second without a key and 10 with one.
        self.min_interval = 0.11 if settings.ncbi_api_key else 0.34
        self._lock = threading.Lock()
        self._last = 0.0

    def _call(self, endpoint: str, data: dict[str, Any]) -> httpx.Response:
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        response = self.http.request("POST", self.BASE + endpoint, data={**self.common, **data})
        if response.status_code != 200:
            raise SourceError(f"PubMed {endpoint} returned HTTP {response.status_code}")
        return response

    def search(self, term: str, retmax: int) -> tuple[int, list[str]]:
        payload = self._call(
            "esearch.fcgi",
            {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json", "sort": "relevance"},
        ).json()
        result = payload.get("esearchresult", {})
        if "ERROR" in result:
            raise SourceError(f"PubMed rejected the query: {result['ERROR']}")
        return int(result.get("count", 0)), list(result.get("idlist", []))

    def summaries(self, pmids: list[str]) -> list[Record]:
        records: list[Record] = []
        for start in range(0, len(pmids), 200):
            chunk = pmids[start : start + 200]
            payload = self._call("esummary.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "json"}).json()
            result = payload.get("result", {})
            for uid in result.get("uids", []):
                item = result.get(uid, {})
                ids = {a.get("idtype"): a.get("value") for a in item.get("articleids", [])}
                records.append(
                    Record(
                        source="pubmed",
                        ext_id=uid,
                        pmid=uid,
                        title=item.get("title", ""),
                        year=_year(item.get("pubdate") or item.get("sortpubdate")),
                        journal=item.get("source", ""),
                        pubtypes=list(item.get("pubtype", [])),
                        doi=ids.get("doi"),
                        pmcid=ids.get("pmc") or ids.get("pmcid"),
                    )
                )
        return records

    def fetch(self, pmids: list[str]) -> list[Record]:
        """Full records, abstracts included."""
        records: list[Record] = []
        for start in range(0, len(pmids), 200):
            chunk = pmids[start : start + 200]
            response = self._call("efetch.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"})
            records.extend(parse_pubmed_xml(response.content))
        return records

    def references(self, pmid: str) -> list[str]:
        """PMIDs an article cites, from its reference list in PubMed Central (none when PMC has no list)."""
        payload = self._call(
            "elink.fcgi",
            {"dbfrom": "pubmed", "db": "pubmed", "id": pmid, "linkname": "pubmed_pubmed_refs", "retmode": "json"},
        ).json()
        for linkset in payload.get("linksets", []):
            for links in linkset.get("linksetdbs", []):
                if links.get("linkname") == "pubmed_pubmed_refs":
                    return [str(x) for x in links.get("links", [])]
        return []


def _text(el: Any) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


# What an affiliation string carries besides the institution: an email address (often after
# "Electronic address:"), and a footnote marker before it ("1Department", "a Department").
_EMAIL_LABEL = re.compile(r"\s*\b(?:electronic address|e-?mail(?: address)?|email)\s*:?\s*(?=\S+@)", re.I)
_EMAIL = re.compile(r"\s*\S+@\S+")
_MARKER = re.compile(r"^(?:\d{1,2}|[a-h])\s?(?=[A-Z][a-z])")


def clean_affiliations(texts: list[Any]) -> list[str]:
    """Affiliations as institutions: split where older records run several together with ";",
    without email addresses or footnote markers, each once and in order. Mirrors cleanAffiliations
    in site/lib/affiliations.js."""
    out: list[str] = []
    for text in texts:
        for raw in re.split(r";\s*", str(text or "")):
            part = _EMAIL.sub("", _EMAIL_LABEL.sub(" ", raw))
            part = _MARKER.sub("", " ".join(part.split()))
            part = re.sub(r"[\s,;:(]+$", "", part).strip()
            if part and part != "." and part not in out:
                out.append(part)
    return out


def short_authors(names: list[str]) -> str:
    """The first three authors, then "et al."; how the overlap check tells reports of one study apart."""
    names = [n for n in names if n]
    return ", ".join(names[:3]) + (", et al." if len(names) > 3 else "")


def parse_pubmed_xml(xml: bytes | str) -> list[Record]:
    root = fromstring(xml)
    records: list[Record] = []
    for article in root.iter("PubmedArticle"):
        citation = article.find("MedlineCitation")
        if citation is None:
            continue
        pmid = (citation.findtext("PMID") or "").strip()
        art = citation.find("Article")
        if art is None or not pmid:
            continue
        parts = []
        for block in art.findall("Abstract/AbstractText"):
            label = block.get("Label")
            text = _text(block)
            if text:
                parts.append(f"{label}: {text}" if label else text)
        year = (
            art.findtext("Journal/JournalIssue/PubDate/Year")
            or _year(art.findtext("Journal/JournalIssue/PubDate/MedlineDate"))
            or art.findtext("ArticleDate/Year")
            or ""
        )
        ids = {
            a.get("IdType"): (a.text or "").strip()
            for a in article.findall("PubmedData/ArticleIdList/ArticleId")
        }
        doi = ids.get("doi")
        if not doi:
            for loc in art.findall("ELocationID"):
                if loc.get("EIdType") == "doi":
                    doi = (loc.text or "").strip()
        names = []
        author_list = []
        for author in art.findall("AuthorList/Author"):
            if author.get("ValidYN") == "N":  # a name published in error; the corrected one is listed too
                continue
            last = (author.findtext("LastName") or "").strip()
            collective = _text(author.find("CollectiveName"))
            names.append(f"{last} {(author.findtext('Initials') or '').strip()}".strip() if last else collective)
            first = _text(author.find("ForeName")) or _text(author.find("Initials"))
            name = f"{first} {_text(author.find('LastName'))}".strip() if last else collective
            affiliations = clean_affiliations([_text(x) for x in author.findall("AffiliationInfo/Affiliation")])
            if name:
                author_list.append({"name": name, "affiliations": affiliations})
        nct_ids = []
        for bank in art.findall("DataBankList/DataBank"):
            if (bank.findtext("DataBankName") or "").strip() == "ClinicalTrials.gov":
                nct_ids += [(n.text or "").strip() for n in bank.findall("AccessionNumberList/AccessionNumber")]
        records.append(
            Record(
                source="pubmed",
                ext_id=pmid,
                pmid=pmid,
                title=_text(art.find("ArticleTitle")),
                authors=short_authors(names),
                year=year,
                journal=art.findtext("Journal/Title") or art.findtext("Journal/ISOAbbreviation") or "",
                pubtypes=[(p.text or "").strip() for p in art.findall("PublicationTypeList/PublicationType")],
                abstract="\n".join(parts),
                doi=doi or None,
                pmcid=ids.get("pmc") or None,
                nct_ids=[n for n in nct_ids if n],
                author_list=author_list,
            )
        )
    return records


_DISPERSION = {
    "STANDARD_DEVIATION": "SD",
    "STANDARD_ERROR": "SE",
    "INTER_QUARTILE_RANGE": "IQR",
    "FULL_RANGE": "range",
    "GEOMETRIC_COEFFICIENT_OF_VARIATION": "geometric CV",
}


class ClinicalTrials:
    BASE = "https://clinicaltrials.gov/api/v2/studies"

    def __init__(self, http: Http):
        self.http = http

    def search(self, term: str, limit: int, with_results: bool = True) -> tuple[int, list[dict]]:
        params: dict[str, Any] = {"query.term": term, "pageSize": min(limit, 100), "countTotal": "true", "format": "json"}
        if with_results:
            params["aggFilters"] = "results:with"
        studies: list[dict] = []
        total = 0
        while len(studies) < limit:
            response = self.http.request("GET", self.BASE, params=params)
            if response.status_code != 200:
                raise SourceError(f"ClinicalTrials.gov returned HTTP {response.status_code}: {response.text[:200]}")
            payload = response.json()
            total = payload.get("totalCount", total)
            studies.extend(payload.get("studies", []))
            token = payload.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return total, studies[:limit]

    def study(self, nct_id: str) -> dict | None:
        response = self.http.request("GET", f"{self.BASE}/{nct_id}", params={"format": "json"})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(f"ClinicalTrials.gov returned HTTP {response.status_code} for {nct_id}")
        return response.json()


def trial_record(study: dict) -> Record:
    protocol = study.get("protocolSection", {})
    ident = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    nct_id = ident.get("nctId", "")
    conditions = protocol.get("conditionsModule", {}).get("conditions", [])
    interventions = [i.get("name", "") for i in protocol.get("armsInterventionsModule", {}).get("interventions", [])]
    outcomes = protocol.get("outcomesModule", {})
    lines = [
        f"Status: {status.get('overallStatus', 'unknown')}; results posted: {'yes' if study.get('hasResults') else 'no'}",
        f"Study type: {design.get('studyType', '')}; allocation: {design.get('designInfo', {}).get('allocation', '')}; "
        f"enrollment: {design.get('enrollmentInfo', {}).get('count', '')}",
        f"Conditions: {', '.join(conditions)}",
        f"Interventions: {', '.join(interventions)}",
    ]
    for kind in ("primaryOutcomes", "secondaryOutcomes"):
        for outcome in outcomes.get(kind, [])[:10]:
            label = "Primary" if kind == "primaryOutcomes" else "Secondary"
            lines.append(f"{label} outcome: {outcome.get('measure', '')} ({outcome.get('timeFrame', '')})")
    summary = protocol.get("descriptionModule", {}).get("briefSummary", "")
    if summary:
        lines.append("Summary: " + " ".join(summary.split()))
    pmids = [
        ref.get("pmid")
        for ref in protocol.get("referencesModule", {}).get("references", [])
        if ref.get("pmid") and ref.get("type") == "RESULT"
    ]
    if pmids:
        lines.append("Result publications (PMID): " + ", ".join(pmids))
    officials = [
        {"name": _clean(o.get("name")), "affiliations": clean_affiliations([o.get("affiliation")]),
         "role": (o.get("role") or "").replace("_", " ").capitalize()}
        for o in protocol.get("contactsLocationsModule", {}).get("overallOfficials", [])
        if _clean(o.get("name"))
    ]
    return Record(
        source="clinicaltrials",
        ext_id=nct_id,
        title=ident.get("officialTitle") or ident.get("briefTitle", ""),
        authors=short_authors([o["name"] for o in officials]),
        year=_year(status.get("startDateStruct", {}).get("date")),
        journal="ClinicalTrials.gov",
        pubtypes=[design.get("studyType", "")] if design.get("studyType") else [],
        abstract="\n".join(lines),
        nct_ids=[nct_id],
        author_list=officials,
    )


def trial_results_text(study: dict) -> str:
    """The posted results of a registry record, written out so each number sits with its labels."""
    record = trial_record(study)
    out = [f"# {record.ext_id}: {record.title}", "", record.abstract, ""]
    results = study.get("resultsSection", {})
    measures = results.get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])
    if not measures:
        out.append("No outcome results are posted for this study.")
    for om in measures:
        groups = {g.get("id"): g.get("title", g.get("id")) for g in om.get("groups", [])}
        out.append(f"## Outcome measure ({om.get('type', '').lower()}): {om.get('title', '')}")
        if om.get("description"):
            out.append("Description: " + " ".join(om["description"].split()))
        out.append(f"Time frame: {om.get('timeFrame', '')}")
        dispersion = om.get("dispersionType", "")
        out.append(
            f"Parameter: {om.get('paramType', '')}; dispersion: {_DISPERSION.get(dispersion, dispersion)}; "
            f"unit: {om.get('unitOfMeasure', '')}"
        )
        for denom in om.get("denoms", []):
            counts = " | ".join(f"{groups.get(c.get('groupId'), c.get('groupId'))}: {c.get('value')}" for c in denom.get("counts", []))
            out.append(f"Analyzed ({denom.get('units', 'participants').lower()}): {counts}")
        for cls in om.get("classes", []):
            for category in cls.get("categories", []):
                label = " / ".join(x for x in (cls.get("title"), category.get("title")) if x)
                cells = []
                for m in category.get("measurements", []):
                    cell = f"{groups.get(m.get('groupId'), m.get('groupId'))}: {m.get('value', '')}"
                    if m.get("spread"):
                        cell += f" ({_DISPERSION.get(dispersion, 'spread')} {m['spread']})"
                    elif m.get("lowerLimit") or m.get("upperLimit"):
                        name = _DISPERSION.get(dispersion, dispersion.replace("_", " ").lower())
                        cell += f" ({name} {m.get('lowerLimit', '')} to {m.get('upperLimit', '')})"
                    cells.append(cell)
                prefix = f"{label}: " if label else "Values: "
                out.append(prefix + " | ".join(cells))
        for analysis in om.get("analyses", []):
            compared = " vs ".join(groups.get(g, g) for g in analysis.get("groupIds", []))
            parts = [f"Analysis ({compared})"]
            if analysis.get("paramType"):
                parts.append(f"{analysis['paramType']} {analysis.get('paramValue', '')}")
            if analysis.get("ciLowerLimit") or analysis.get("ciUpperLimit"):
                parts.append(
                    f"{analysis.get('ciPctValue', '')}% CI {analysis.get('ciLowerLimit', '')} to {analysis.get('ciUpperLimit', '')}"
                )
            if analysis.get("pValue"):
                parts.append(f"p = {analysis['pValue']}")
            if analysis.get("statisticalMethod"):
                parts.append(f"method: {analysis['statisticalMethod']}")
            out.append("; ".join(parts))
        out.append("")
    return "\n".join(out).strip() + "\n"


class EuropePMC:
    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/"

    def __init__(self, http: Http):
        self.http = http

    def search(self, query: str, limit: int, abstracts: bool = True) -> tuple[int, list[dict]]:
        """Results of a Europe PMC query, by relevance: PubMed records (source MED), articles only in
        PubMed Central (PMC) and preprints (PPR). Other kinds (patents, theses, Agricola) are left out.
        Europe PMC searches the full text of open-access articles too, section by section."""
        params = {"query": query, "format": "json", "pageSize": max(1, min(limit, 1000)),
                  "resultType": "core" if abstracts else "lite"}
        response = self.http.request("GET", self.BASE + "search", params=params)
        if response.status_code != 200:
            raise SourceError(f"Europe PMC returned HTTP {response.status_code}")
        payload = response.json()
        results = (payload.get("resultList") or {}).get("result") or []
        kept = [r for r in results if r.get("source") in EUROPEPMC_KINDS and r.get("id")]
        return int(payload.get("hitCount") or 0), kept[:limit]

    def fulltext_xml(self, pmcid: str) -> str | None:
        pmcid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
        response = self.http.request("GET", f"{self.BASE}{pmcid}/fullTextXML")
        if response.status_code == 200 and response.text.lstrip().startswith("<"):
            return response.text
        return None

    def references(self, pmid: str) -> list[str]:
        """PMIDs an article cites, as far as Europe PMC matched its reference list to PubMed."""
        response = self.http.request(
            "GET", f"{self.BASE}MED/{pmid}/references", params={"page": 1, "pageSize": 1000, "format": "json"}
        )
        if response.status_code != 200:
            return []
        found = (response.json().get("referenceList") or {}).get("reference") or []
        return [str(r["id"]) for r in found if r.get("source") == "MED" and r.get("id")]


EUROPEPMC_KINDS = {"MED", "PMC", "PPR"}


def _plain(text: str | None) -> str:
    """Europe PMC titles and abstracts carry HTML tags (<i>, <h4>Background</h4>)."""
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def europepmc_pmid(result: dict) -> str | None:
    return str(result.get("pmid") or (result["id"] if result.get("source") == "MED" else "")) or None


def europepmc_authors(result: dict) -> list[dict]:
    """A Europe PMC result's authors with their affiliations; from the names alone when the result
    has no author list (a "lite" result)."""
    out = []
    for a in (result.get("authorList") or {}).get("author") or []:
        first = _clean(a.get("firstName")) or _clean(a.get("initials"))  # as PubMed's: the initials when there is no first name
        last = _clean(a.get("lastName"))
        name = _clean(f"{first} {last}") if last else _clean(a.get("fullName")) or _clean(a.get("collectiveName"))
        details = (a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []
        affiliations = clean_affiliations([d.get("affiliation") for d in details]) or clean_affiliations([a.get("affiliation")])
        if name:
            out.append({"name": name, "affiliations": affiliations})
    if not out:
        out = [{"name": n, "affiliations": []} for n in _europepmc_names(result)]
    return out


def _europepmc_names(result: dict) -> list[str]:
    return [n for n in (_clean(x).rstrip(".") for x in (result.get("authorString") or "").split(",")) if n]


def europepmc_record(result: dict) -> Record:
    """A Europe PMC result that is not in PubMed, as a record."""
    journal = ((result.get("journalInfo") or {}).get("journal") or {}).get("title") or (
        (result.get("bookOrReportDetails") or {}).get("publisher") or ""
    )
    pubtypes = (result.get("pubTypeList") or {}).get("pubType") or []
    return Record(
        source="europepmc",
        ext_id=str(result["id"]),
        title=_plain(result.get("title")),
        authors=short_authors(_europepmc_names(result)),
        year=_year(str(result.get("pubYear") or "")),
        journal=journal,
        pubtypes=[pubtypes] if isinstance(pubtypes, str) else list(pubtypes),
        abstract=_plain(result.get("abstractText")),
        doi=result.get("doi"),
        pmcid=result.get("pmcid"),
        author_list=europepmc_authors(result),
    )


def europepmc_url(ext_id: str) -> str:
    kind = "PPR" if ext_id.startswith("PPR") else "PMC" if ext_id.startswith("PMC") else "MED"
    return f"https://europepmc.org/article/{kind}/{ext_id}"


class Unpaywall:
    BASE = "https://api.unpaywall.org/v2/"

    def __init__(self, http: Http, email: str | None):
        self.http = http
        self.email = email

    def pdf_url(self, doi: str) -> str | None:
        if not self.email:
            return None
        response = self.http.request("GET", self.BASE + quote(doi, safe="/"), params={"email": self.email})
        if response.status_code != 200:
            return None
        payload = response.json()
        locations = [payload.get("best_oa_location") or {}] + list(payload.get("oa_locations") or [])
        for location in locations:
            if location and location.get("url_for_pdf"):
                return location["url_for_pdf"]
        return None

    def download_pdf(self, url: str) -> bytes | None:
        try:
            with self.http.client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        return None
                    chunks.append(chunk)
        except httpx.HTTPError:
            return None
        data = b"".join(chunks)
        return data if data.startswith(b"%PDF") else None


class Sources:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.http = Http(settings, client)
        self.pubmed = PubMed(self.http, settings)
        self.trials = ClinicalTrials(self.http)
        self.europepmc = EuropePMC(self.http)
        self.unpaywall = Unpaywall(self.http, settings.contact_email)

    def references(self, pmid: str) -> list[str]:
        """PMIDs cited by an article: Europe PMC's reference list, else PubMed Central's."""
        try:
            found = self.europepmc.references(pmid)
        except (SourceError, ValueError):
            found = []
        return found or self.pubmed.references(pmid)
