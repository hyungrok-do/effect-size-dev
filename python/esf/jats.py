"""Turn JATS full-text XML (PubMed Central, Europe PMC) into plain text that keeps tables readable.

Each table row becomes one line with cells separated by " | ", so a quote of a whole row can be
found verbatim in the text.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring

_FLOATS = {"table-wrap", "fig", "table-wrap-group", "fig-group"}
_SKIP = {"ref-list", "fn-group", "ack", "glossary", "notes", "xref-list"}


def _strip_namespaces(root: Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _clean(text: str) -> str:
    return " ".join(text.split())


def _inline_text(el: Element) -> str:
    """Text of an element, leaving out floats (tables, figures) nested inside it."""
    parts = [el.text or ""]
    for child in el:
        if child.tag not in _FLOATS:
            parts.append(_inline_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _table(wrap: Element, out: list[str]) -> None:
    label = _clean(wrap.findtext("label") or "")
    caption = wrap.find("caption")
    caption_text = _clean(_inline_text(caption)) if caption is not None else ""
    out.append(f"[{label or 'Table'}] {caption_text}".rstrip())
    for table in wrap.iter("table"):
        for tr in table.iter("tr"):
            cells = [_clean("".join(cell.itertext())) for cell in tr if cell.tag in ("td", "th")]
            if any(cells):
                out.append(" | ".join(cells))
    foot = wrap.find("table-wrap-foot")
    if foot is not None:
        note = _clean("".join(foot.itertext()))
        if note:
            out.append(f"Table note: {note}")
    out.append("")


def _figure(fig: Element, out: list[str]) -> None:
    label = _clean(fig.findtext("label") or "Figure")
    caption = fig.find("caption")
    caption_text = _clean("".join(caption.itertext())) if caption is not None else ""
    out.append(f"[{label}] {caption_text} (figure image not included)")
    out.append("")


def _walk(el: Element, out: list[str], depth: int) -> None:
    for child in el:
        tag = child.tag
        if tag in _SKIP:
            continue
        if tag == "sec":
            title = child.find("title")
            if title is not None:
                out.append("#" * min(depth + 2, 6) + " " + _clean("".join(title.itertext())))
            _walk(child, out, depth + 1)
        elif tag == "title":
            continue
        elif tag == "p":
            text = _clean(_inline_text(child))
            if text:
                out.append(text)
                out.append("")
            for float_el in child:
                if float_el.tag in _FLOATS:
                    _walk_float(float_el, out)
        elif tag in _FLOATS:
            _walk_float(child, out)
        elif tag == "list":
            for item in child.iter("list-item"):
                text = _clean(_inline_text(item))
                if text:
                    out.append(f"- {text}")
            out.append("")
        else:
            _walk(child, out, depth)


def _walk_float(el: Element, out: list[str]) -> None:
    if el.tag == "table-wrap":
        _table(el, out)
    elif el.tag == "fig":
        _figure(el, out)
    else:
        for child in el:
            _walk_float(child, out)


def jats_to_text(xml: str | bytes) -> str:
    root = fromstring(xml)
    _strip_namespaces(root)
    out: list[str] = []

    title = root.find("./front/article-meta/title-group/article-title")
    if title is not None:
        out.append("# " + _clean("".join(title.itertext())))
        out.append("")

    for abstract in root.findall("./front/article-meta/abstract"):
        if abstract.get("abstract-type") in ("graphical", "teaser"):
            continue
        out.append("## Abstract")
        _walk(abstract, out, 1)
        text = _clean(abstract.text or "")
        if text:
            out.append(text)
        out.append("")

    body = root.find("body")
    if body is not None:
        _walk(body, out, 0)

    floats = root.find("floats-group")
    if floats is not None:
        for child in floats:
            _walk_float(child, out)

    back = root.find("back")
    if back is not None:
        for app in back.iter("app"):
            _walk(app, out, 1)

    return "\n".join(out).strip() + "\n"
