"""Modeles de reponse Pydantic pour l'API."""

from pydantic import BaseModel, Field


# ---- Search ----

class Snippet(BaseModel):
    text: str
    highlights: list[tuple[int, int]] = Field(default_factory=list)


class SearchHit(BaseModel):
    score: float
    source: str | None = None
    filename: str | None = None
    type: str | None = None
    snippets: list[Snippet] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    total: int
    limit: int
    offset: int
    has_more: bool
    total_pages: int
    deduplicated: bool = False
    total_is_raw: bool = False
    index_id: str
    index_name: str = ""
    hits: list[SearchHit] = Field(default_factory=list)


# ---- Indexes ----

class IndexInfo(BaseModel):
    id: str
    name: str
    path: str
    source: str = ""
    status: str = "ready"
    error: str = ""
    last_error_at: float = 0
    retry_count: int = 0
    next_retry_at: float = 0
    retry_delay_seconds: int = 0
    last_ready_at: float = 0


class AddIndexResponse(BaseModel):
    status: str = "ok"
    id: str
    name: str


# ---- Stats ----

class SegmentDetail(BaseModel):
    id: str
    docs: int
    index_id: str = ""


class EmailStats(BaseModel):
    unique_count: int = 0
    total_occurrences: int = 0
    perso_count: int = 0
    pro_count: int = 0
    top_emails: list[dict] = Field(default_factory=list)
    top_domains: list[dict] = Field(default_factory=list)
    tld_distribution: list[dict] = Field(default_factory=list)


class PhoneStats(BaseModel):
    unique_count: int = 0
    total_occurrences: int = 0
    top_phones: list[dict] = Field(default_factory=list)


class AnalysisLimits(BaseModel):
    email_counter_limit: int = 0
    phone_counter_limit: int = 0


class AnalysisData(BaseModel):
    scan_date: str = ""
    scan_duration_seconds: float = 0
    source_dir: str = ""
    total_files: int = 0
    files_processed: int = 0
    total_lines: int = 0
    complete: bool = False
    failed_files: list[dict] = Field(default_factory=list)
    limits: AnalysisLimits = Field(default_factory=AnalysisLimits)
    emails: EmailStats = Field(default_factory=EmailStats)
    phones: PhoneStats = Field(default_factory=PhoneStats)
    sources: list[dict] = Field(default_factory=list)


class SkippedIndex(BaseModel):
    id: str
    name: str
    status: str


class StatsResponse(BaseModel):
    total_docs: int
    num_segments: int
    index_size_bytes: int = 0
    index_size: str = ""
    index_size_pending: bool = False
    segments: list[SegmentDetail] = Field(default_factory=list)
    index_id: str = ""
    index_name: str = ""
    analysis: AnalysisData | None = None
    skipped_indexes: list[SkippedIndex] = Field(default_factory=list)


# ---- Health ----

class HealthResponse(BaseModel):
    status: str
    ready: bool = False
    indexes_loaded: int
    indexes: list[str] = Field(default_factory=list)
    indexes_ready: list[str] = Field(default_factory=list)
    indexes_loading: list[str] = Field(default_factory=list)
    indexes_error: list[str] = Field(default_factory=list)


# ---- Analyze ----

class AnalyzeResponse(BaseModel):
    status: str


class AnalyzeStatus(BaseModel):
    running: bool = False
    progress: int = 0
    total_files: int = 0
    current_file: str = ""
    last_run: str | None = None
    emails_found: int = 0
    phones_found: int = 0


# ---- Breach Lookup (agrege) ----

class HibpDetailsModel(BaseModel):
    """Champs specifiques HIBP."""

    title: str = ""
    added_date: str = ""
    modified_date: str = ""
    pwn_count: int = 0
    is_verified: bool = False
    is_fabricated: bool = False
    is_sensitive: bool = False
    is_retired: bool = False
    is_spam_list: bool = False
    is_malware: bool = False
    is_subscription_free: bool = False
    is_stealer_log: bool = False
    attribution: str = ""
    disclosure_url: str = ""


class BdEntryModel(BaseModel):
    """Credential individuel BreachDirectory."""

    source: str = ""
    password: str = ""
    hash_password: bool = False
    sha1: str = ""
    hash: str = ""
    has_password: bool = False


class BreachResultModel(BaseModel):
    name: str
    domain: str = ""
    breach_date: str = ""
    description: str = ""
    data_classes: list[str] = Field(default_factory=list)
    source_provider: str = ""
    logo_url: str = ""
    password_exposed: bool = False
    hibp_details: HibpDetailsModel | None = None
    bd_entries: list[BdEntryModel] = Field(default_factory=list)


class ProviderResponseModel(BaseModel):
    name: str
    ok: bool
    results: list[BreachResultModel] = Field(default_factory=list)
    error: str = ""


class BreachSummary(BaseModel):
    providers_ok: int = 0
    providers_failed: int = 0
    total_hits: int = 0


class BreachesResponse(BaseModel):
    query: str
    providers: list[ProviderResponseModel] = Field(default_factory=list)
    summary: BreachSummary = Field(default_factory=BreachSummary)
