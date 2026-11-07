"""AI generation pipeline: build sources -> (chunk + map) -> generate -> validate."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable

from ..config import settings
from ..scraping.extractor import document_text
from .prompts import BASE_SYSTEM, MAP_SYSTEM, TITLES, user_prompt
from .providers import AIError, get_provider

# phrases that suggest a page is trying to instruct an AI (flagged to the user, never obeyed)
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the|all|your) "
    r"(previous|prior|above|system)|you are now (a|an|in)|system prompt|reveal (your|the) (prompt|instructions|api key)|"
    r"\bjailbreak\b|developer mode|do anything now|<\s*/?\s*(system|assistant)\s*>)",
    re.I,
)


@dataclass
class Source:
    sid: str
    url: str
    title: str
    text: str
    outline: list[str] = field(default_factory=list)


@dataclass
class AIResult:
    markdown: str
    warnings: list[str]
    stats: dict


def build_sources(docs: list[dict], include_tables: bool = True) -> list[Source]:
    out = []
    for i, d in enumerate(docs, 1):
        text = document_text(d, with_tables=False)
        if include_tables and d.get("tables"):
            for t in d["tables"]:
                text += "\n\n" + md_table(t)
        if d.get("rows"):
            text += "\n\nExtracted records:\n" + "\n".join(
                "- " + "; ".join(f"{k}: {v}" for k, v in r.items()) for r in d["rows"][:300])
        outline = [("  " * (h["level"] - 1)) + h["text"] for h in d.get("headings", [])][:60]
        out.append(Source(f"S{i}", d["source_url"], d.get("title") or d["source_url"], text, outline))
    return out


def md_table(t: dict) -> str:
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(esc(h) for h in t["headers"]) + " |",
             "|" + "---|" * len(t["headers"])]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in t["rows"][:200]]
    return ("**" + t["caption"] + "**\n\n" if t.get("caption") else "") + "\n".join(lines)


def _wrap(src: Source, text: str, part: str = "") -> str:
    # neutralise attempts to close the wrapper from inside the page
    safe = re.sub(r"<\s*/?\s*source", "‹source", text, flags=re.I)
    attrs = f'id="{src.sid}" url="{src.url}" title="{_attr(src.title)}"' + (f' part="{part}"' if part else "")
    return f"<source {attrs}>\n{safe}\n</source>"


def _attr(s: str) -> str:
    return s.replace('"', "'").replace("<", "‹").replace(">", "›")[:200]


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split on headings, then paragraphs, keeping chunks under max_chars."""
    if len(text) <= max_chars:
        return [text]
    sections = re.split(r"(?m)^(?=#{1,4} )", text)
    chunks: list[str] = []
    cur = ""
    for sec in sections:
        if len(sec) > max_chars:
            for para in re.split(r"\n{2,}", sec):
                while len(para) > max_chars:  # very long paragraph
                    chunks.append(cur) if cur else None
                    cur = ""
                    chunks.append(para[:max_chars])
                    para = para[max_chars:]
                if len(cur) + len(para) + 2 > max_chars and cur:
                    chunks.append(cur)
                    cur = ""
                cur += para + "\n\n"
        elif len(cur) + len(sec) > max_chars and cur:
            chunks.append(cur)
            cur = sec
        else:
            cur += sec
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def detect_injection(sources: list[Source]) -> list[str]:
    hits = []
    for s in sources:
        m = _INJECTION.search(s.text)
        if m:
            hits.append(f"{s.sid} ({s.url}) contains text that looks like instructions to an AI "
                        f"(\"{m.group(0)[:60]}\"). It was treated as plain content and not followed.")
    return hits


async def _map_chunks(provider, sources: list[Source], progress: Callable[[str], None]) -> str:  # noqa: ANN001
    tasks = []
    for s in sources:
        parts = chunk_text(s.text, settings.ai_chunk_chars)
        for i, part in enumerate(parts, 1):
            ctx = (f"Context: this is part {i} of {len(parts)} of the page \"{s.title}\".\n"
                   f"Page outline:\n" + "\n".join(s.outline[:40]) + "\n\n") if len(parts) > 1 else ""
            tasks.append((s, i, len(parts), ctx + _wrap(s, part, f"{i}/{len(parts)}")))
    sem = asyncio.Semaphore(4)
    done = 0

    async def run(item):  # noqa: ANN001
        nonlocal done
        s, i, n, prompt = item
        async with sem:
            out = await provider.complete(MAP_SYSTEM, prompt, fast=True, max_tokens=3000, temperature=0)
        done += 1
        progress(f"Reading content ({done}/{len(tasks)} chunks)")
        return s.sid, i, out

    results = await asyncio.gather(*(run(t) for t in tasks))
    by_source: dict[str, list[str]] = {}
    for sid, i, out in sorted(results, key=lambda r: (int(r[0][1:]), r[1])):
        if out.strip() and out.strip() != "(empty)":
            by_source.setdefault(sid, []).append(out.strip())
    notes = []
    for s in sources:
        if s.sid in by_source:
            body = _dedupe_bullets("\n".join(by_source[s.sid]))
            notes.append(_wrap(s, "Notes extracted from this page:\n" + body))
    return "\n\n".join(notes)


def _dedupe_bullets(text: str) -> str:
    seen, out = set(), []
    for line in text.splitlines():
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if line.strip().startswith(("-", "*")) and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _strip_wrappers(md: str) -> str:
    md = md.strip()
    md = re.sub(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", r"\1", md, flags=re.S)
    # drop any Sources section the model added despite instructions
    md = re.split(r"(?im)^#{1,3}\s*(sources|references)\s*$", md)[0].rstrip()
    return md


_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{2,})(?![\w])")


def validate(md: str, sources: list[Source]) -> tuple[str, list[str], dict]:
    """Post-checks: invalid citations, uncited numbers, coverage. Returns cleaned md + warnings."""
    warnings: list[str] = []
    valid = {s.sid for s in sources}
    cited = set(re.findall(r"\[(S\d+)\]", md))
    bad = cited - valid
    if bad:
        md = re.sub(r"\[(S\d+)\]", lambda m: m.group(0) if m.group(1) in valid else "", md)
        warnings.append(f"Removed citations to sources that don't exist: {', '.join(sorted(bad))}.")
    # numbers that appear in the output but nowhere in the sources (possible hallucination)
    corpus = " ".join(s.text for s in sources).replace(",", "")
    body = re.sub(r"\[S\d+\]", "", md)
    body = re.sub(r"\(https?://[^)]+\)", "", body)
    missing = []
    for n in dict.fromkeys(_NUM.findall(body)):
        plain = n.replace(",", "")
        if plain not in corpus and not re.fullmatch(r"(19|20)\d\d", plain):
            missing.append(n)
    if missing:
        warnings.append("These figures don't appear in the scraped content. Check them before use: "
                        + ", ".join(missing[:12]) + ("…" if len(missing) > 12 else ""))
    paras = [p for p in re.split(r"\n{2,}", md) if p.strip() and not p.lstrip().startswith(("#", "|", "```", ">"))
             and "AI interpretation" not in p]
    cited_paras = sum(1 for p in paras if re.search(r"\[S\d+\]", p))
    coverage = round(cited_paras / len(paras), 2) if paras else 1.0
    if paras and coverage < 0.4:
        warnings.append(f"Only {int(coverage * 100)}% of paragraphs cite a source. Review for unsupported claims.")
    return md, warnings, {"citation_coverage": coverage, "sources_cited": sorted(cited & valid)}


def sources_section(sources: list[Source], cited: list[str]) -> str:
    use = [s for s in sources if s.sid in cited] or sources
    lines = ["## Sources", ""]
    lines += [f"- **[{s.sid}]** [{s.title}]({s.url})" for s in use]
    return "\n".join(lines)


async def generate(output_type: str, docs: list[dict], opts: dict,
                   progress: Callable[[str], None] = lambda _: None) -> AIResult:
    provider = get_provider()
    sources = build_sources(docs, opts.get("include_tables", True))
    total = sum(len(s.text) for s in sources)
    if total == 0:
        raise AIError("bad_output", "(the scraped pages contain no text)")
    warnings = detect_injection(sources)
    if total > settings.ai_max_total_chars:
        # hard budget: trim the longest sources proportionally
        ratio = settings.ai_max_total_chars / total
        for s in sources:
            s.text = s.text[: int(len(s.text) * ratio)]
        warnings.append(f"Content was {total:,} characters; only the first {int(ratio * 100)}% of each "
                        "page was used to stay within the AI budget.")
        total = sum(len(s.text) for s in sources)

    strategy = "direct"
    if total <= settings.ai_chunk_chars * 1.2:
        material = "\n\n".join(_wrap(s, s.text) for s in sources)
    else:
        strategy = "map-reduce"
        progress("Reading content in chunks")
        material = await _map_chunks(provider, sources, progress)
        # second-level reduction if the notes themselves are still too long
        if len(material) > settings.ai_chunk_chars * 3:
            progress("Condensing notes")
            note_src = [Source(s.sid, s.url, s.title, t) for s, t in
                        zip(sources, re.findall(r"<source[^>]*>\n(.*?)\n</source>", material, re.S))]
            material = await _map_chunks(provider, note_src, progress)

    progress("Writing output")
    target = opts.get("length_words") or {"brief": 350, "standard": 900, "comprehensive": 2200}[opts.get("detail", "standard")]
    max_tokens = min(16000, max(1500, int(target * 2.2) + 800))
    prompt = user_prompt(output_type, opts, material, len(sources))
    md = await provider.complete(BASE_SYSTEM, prompt, max_tokens=max_tokens)
    md = _strip_wrappers(md)
    if len(md) < 40:
        raise AIError("bad_output", "(the response was empty)")
    if not md.lstrip().startswith("# "):
        md = f"# {opts.get('title') or TITLES[output_type]}\n\n{md}"

    progress("Checking accuracy")
    md, v_warn, v_stats = validate(md, sources)
    warnings += v_warn
    if opts.get("include_sources", True):
        md += "\n\n" + sources_section(sources, v_stats["sources_cited"])
    return AIResult(md, warnings, {"strategy": strategy, "input_chars": total,
                                   "provider": provider.name, **v_stats})


def split_sections(md: str) -> list[tuple[str, str]]:
    """[(heading_line, body)] for '## ' sections. The preamble has heading ''."""
    parts = re.split(r"(?m)^(## .+)$", md)
    out = [("", parts[0])]
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1] if i + 1 < len(parts) else ""))
    return out


async def regenerate_section(md: str, section: str, docs: list[dict], opts: dict,
                             extra: str | None) -> tuple[str, list[str]]:
    provider = get_provider()
    sections = split_sections(md)
    idx = next((i for i, (h, _) in enumerate(sections) if h[3:].strip().lower() == section.strip().lower()), None)
    if idx is None:
        raise AIError("bad_output", f"(section '{section}' not found)")
    sources = build_sources(docs, opts.get("include_tables", True))
    material = "\n\n".join(_wrap(s, s.text[: settings.ai_chunk_chars]) for s in sources)
    heading, body = sections[idx]
    prompt = (f"Rewrite ONLY the section below of an existing document. Return the section starting with "
              f"its '## ' heading line and nothing else.\n\nCurrent section:\n{heading}\n{body.strip()}\n\n"
              + (f"<user_instructions>\n{extra}\n</user_instructions>\n\n" if extra else "")
              + f"Style: {opts.get('style', 'professional')}. Source material:\n\n{material}")
    new = _strip_wrappers(await provider.complete(BASE_SYSTEM, prompt, max_tokens=4000))
    new = re.sub(r"^#\s.+\n", "", new).strip()
    if not new.startswith("## "):
        new = heading + "\n\n" + new
    new_md_parts = []
    for i, (h, b) in enumerate(sections):
        new_md_parts.append(new + "\n\n" if i == idx else (h + b if h else b))
    out, warnings, _ = validate("".join(new_md_parts).strip(), sources)
    return out, warnings
