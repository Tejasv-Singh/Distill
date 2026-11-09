"""API request/response schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .security import UnsafeURL, normalize_url

RenderMode = Literal["auto", "static", "js"]


class FieldSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[\w .-]+$")
    selector: str = Field(..., min_length=1, max_length=500)
    attr: str = Field("text", max_length=64)        # text | html | href | src | any attribute
    multiple: bool = False


class DataExtraction(BaseModel):
    """Optional 'precise mode': CSS selectors -> rows, alongside the document."""
    item_selector: Optional[str] = Field(None, max_length=500)
    fields: list[FieldSpec] = Field(default_factory=list, max_length=40)


class CrawlConfig(BaseModel):
    max_pages: int = Field(10, ge=1, le=500)
    max_depth: int = Field(1, ge=0, le=10)
    include_patterns: list[str] = Field(default_factory=list, max_length=20)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=20)
    same_domain: bool = True
    use_sitemap: bool = False
    next_page_selector: Optional[str] = Field(None, max_length=300)
    link_selector: Optional[str] = Field(None, max_length=300)
    delay_seconds: float = Field(1.0, ge=0, le=30)
    respect_robots: bool = True


class ScrapeRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=50)
    name: Optional[str] = Field(None, max_length=200)
    mode: Literal["single", "crawl"] = "single"
    render: RenderMode = "auto"
    wait_for_selector: Optional[str] = Field(None, max_length=300)
    scroll_steps: int = Field(0, ge=0, le=30)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    data_extraction: Optional[DataExtraction] = None

    @field_validator("urls")
    @classmethod
    def _validate_urls(cls, v: list[str]) -> list[str]:
        out, errors = [], []
        for raw in v:
            if not raw.strip():
                continue
            try:
                out.append(normalize_url(raw))
            except UnsafeURL as e:
                errors.append(f"{raw[:100]}: {e}")
        if errors:
            raise ValueError("; ".join(errors))
        if not out:
            raise ValueError("Enter at least one URL.")
        return list(dict.fromkeys(out))


class DiscoverRequest(BaseModel):
    url: str
    use_sitemap: bool = True
    limit: int = Field(200, ge=1, le=1000)

    @field_validator("url")
    @classmethod
    def _v(cls, v: str) -> str:
        try:
            return normalize_url(v)
        except UnsafeURL as e:
            raise ValueError(str(e)) from e


class ProjectPatch(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


CONVERT_FORMATS = ["pdf", "docx", "markdown", "txt", "html", "csv", "xlsx", "json", "xml", "pptx"]
AI_TYPES = ["detailed_summary", "executive_summary", "research_report", "technical_documentation",
            "study_notes", "blog_post", "newsletter", "comparison_report", "key_insights", "faq",
            "presentation", "custom"]
DOC_FORMATS = ["markdown", "pdf", "docx", "html", "txt", "pptx"]


class OutputOptions(BaseModel):
    # documents
    title: Optional[str] = Field(None, max_length=200)
    include_images: bool = True
    include_tables: bool = True
    include_sources: bool = True
    include_toc: bool = False
    # spreadsheets
    sheet_source: Literal["auto", "tables", "content", "links", "rows"] = "auto"
    combine_pages: bool = True
    normalize_tables: bool = True
    columns: list[str] = Field(default_factory=list, max_length=50)
    # presentations
    slide_count: int = Field(10, ge=3, le=40)
    theme: Literal["light", "dark"] = "light"
    include_charts: bool = True
    # AI
    style: Literal["professional", "academic", "casual", "technical"] = "professional"
    detail: Literal["brief", "standard", "comprehensive"] = "standard"
    length_words: Optional[int] = Field(None, ge=50, le=8000)
    audience: Optional[str] = Field(None, max_length=200)
    tone: Optional[str] = Field(None, max_length=100)
    focus: Optional[str] = Field(None, max_length=500)
    instructions: Optional[str] = Field(None, max_length=4000)   # custom output / extra guidance
    page_ids: list[str] = Field(default_factory=list)            # empty = all pages


class OutputItem(BaseModel):
    kind: Literal["convert", "ai"]
    format: str
    download_format: Optional[str] = None  # for AI outputs: which file to render (default markdown)

    @field_validator("format")
    @classmethod
    def _f(cls, v: str, info) -> str:  # noqa: ANN001
        return v


class GenerateRequest(BaseModel):
    items: list[OutputItem] = Field(..., min_length=1, max_length=25)
    options: OutputOptions = Field(default_factory=OutputOptions)

    @field_validator("items")
    @classmethod
    def _items(cls, v: list[OutputItem]) -> list[OutputItem]:
        for it in v:
            if it.kind == "convert" and it.format not in CONVERT_FORMATS:
                raise ValueError(f"Unknown format '{it.format}'.")
            if it.kind == "ai" and it.format not in AI_TYPES:
                raise ValueError(f"Unknown AI output '{it.format}'.")
            if it.download_format and it.download_format not in DOC_FORMATS:
                raise ValueError(f"'{it.download_format}' isn't available for AI outputs.")
        return v


class OutputEdit(BaseModel):
    content: str = Field(..., max_length=2_000_000)
    title: Optional[str] = Field(None, max_length=200)


class RenderRequest(BaseModel):
    format: Literal["markdown", "pdf", "docx", "html", "txt", "pptx"]


class RegenerateRequest(BaseModel):
    section: Optional[str] = Field(None, max_length=300)   # heading text of the section to redo
    instructions: Optional[str] = Field(None, max_length=2000)


class ChatMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
