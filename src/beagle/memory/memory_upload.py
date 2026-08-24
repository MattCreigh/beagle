"""Memory Upload Procedure — self-prompted "remember this", pipeline-fed into RAG.

workstream: tide.comet.amber (D7)

This module is the runtime for the Memory Upload Procedure: a deliberate,
gated path that distils a small number of high-signal knowledge points from
free text and pushes them into inline RAG / hierarchical memory — the same
instinct as Claude's "remember this" behaviour, but with explicit gates:

  1. significance  — only worth-remembering points pass (memory_upload.toml)
  2. secrets       — every point is scrubbed; still-matching points are dropped
  3. dedup         — points RAG already knows are skipped
  4. cap           — never upload more than N points in one pass (anti-flood)

DESIGN PRINCIPLES
  - The markdown corpus file is the durable artefact. RAG ingest and the
    hierarchical store are best-effort optimisations layered on top, so the
    procedure degrades gracefully when the MCP servers are down.
  - The behavioural thresholds are owned by ``memory_upload.toml`` (the SSOT);
    the defaults here mirror it and must not silently diverge (R33).
  - No hard dependency on the RAG/MCP stack at import time — integrations are
    lazy-imported inside methods so the module is unit-testable in isolation.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("Beagle.memory_upload")

# Defaults mirror memory_upload.toml [significance]/[dedup]. Kept in sync with
# that SSOT; the TOML wins if a loader overrides these.
_DEFAULT_MIN_SCORE = 0.55
_DEFAULT_MIN_CHARS = 40
_DEFAULT_MAX_CHARS = 1200
_DEFAULT_MAX_POINTS = 5
_DEFAULT_DEDUP_THRESHOLD = 0.85

# Heuristic significance signals (mirror the TOML positive/negative_signals).
_POSITIVE_PATTERNS = (
    r"\bbecause\b",
    r"\bso that\b",
    r"\bprefer\b",
    r"\bnever\b",
    r"\balways\b",
    r"\bgotcha\b",
    r"\bconstraint\b",
    r"\bdecided?\b",
    r"\brationale\b",
    r"\bfix(ed|es)?\b",
    r"\bwrong\b",
    r"\bcorrect(ed|ion)?\b",
    r"[\w/]+\.py:\d+",
    r"\bdo not\b",
    r"\bmust\b",
)
_NEGATIVE_PATTERNS = (
    r"^\s*(running|reading|loading|checking|let me|i will|i'll)\b",
    r"\b(ok|okay|sure|thanks|got it|sounds good)\b\s*$",
)

# Last-line-of-defence secret patterns, used only if the canonical scrubber
# (beagle.security.sanitization.scrub_secrets) is unavailable at runtime.
_FALLBACK_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN[ A-Z]+PRIVATE KEY-----"  # PEM private keys
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"  # bearer tokens
    r"|(?i:api[_-]?key|secret|token)\s*[:=]\s*\S{12,}"  # key=... assignments
    r"|sk-[A-Za-z0-9]{20,}"  # OpenAI-style keys
    r"|\b[A-Fa-f0-9]{40,}\b"  # long hex blobs
    r")"
)


@dataclass(slots=True)
class MemoryUploadConfig:
    """Upload gates. Defaults mirror ``memory_upload.toml``."""

    min_score: float = _DEFAULT_MIN_SCORE
    min_chars: int = _DEFAULT_MIN_CHARS
    max_chars: int = _DEFAULT_MAX_CHARS
    max_points_per_upload: int = _DEFAULT_MAX_POINTS
    dedup_threshold: float = _DEFAULT_DEDUP_THRESHOLD
    dedup_enabled: bool = True
    workstream: str = "tide.comet.amber"
    corpus_dir: Path | None = None  # resolved lazily to get_memory_dir()/uploads


@dataclass(slots=True)
class MemoryPoint:
    """A single distilled knowledge point."""

    text: str
    significance: float = 0.0
    source: str = "session"
    scrubbed: bool = False
    dedup_checked: bool = True
    point_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_markdown(self, workstream: str) -> str:
        """Render the point as a provenance-stamped markdown record."""
        ts = datetime.now(UTC).isoformat()
        return (
            f"<!-- memory_point id={self.point_id} workstream={workstream} -->\n"
            f"# Memory: {self.source}\n\n"
            f"- **uploaded_at_utc:** {ts}\n"
            f"- **significance:** {self.significance:.2f}\n"
            f"- **scrubbed:** {self.scrubbed}\n"
            f"- **dedup_checked:** {self.dedup_checked}\n\n"
            f"{self.text.strip()}\n"
        )


@dataclass(slots=True)
class UploadResult:
    """Outcome of an upload pass."""

    uploaded: list[MemoryPoint] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (text, reason)
    corpus_paths: list[str] = field(default_factory=list)
    rag_ingested: bool = False
    memory_stored: int = 0


# ── Redaction (secrets gate, risk R4) ─────────────────────────────────────────


def redact(text: str) -> tuple[str, bool]:
    """Scrub secrets from ``text``.

    Uses the canonical ``beagle.security.sanitization.scrub_secrets`` when
    importable; otherwise falls back to a conservative local regex. Returns
    ``(scrubbed_text, still_matches)`` where ``still_matches`` is True if a
    secret pattern survives scrubbing (such points must be dropped, not uploaded).
    """
    scrubbed = text
    try:
        from beagle.security.sanitization import scrub_secrets

        scrubbed = scrub_secrets(text)
    except ImportError:
        scrubbed = _FALLBACK_SECRET_RE.sub("[REDACTED]", text)
    except (RuntimeError, ValueError) as exc:  # scrubber misbehaved — fail closed
        logger.warning("[MemoryUpload] scrub_secrets failed (%s); using fallback", exc)
        scrubbed = _FALLBACK_SECRET_RE.sub("[REDACTED]", text)

    still_matches = bool(_FALLBACK_SECRET_RE.search(scrubbed))
    return scrubbed, still_matches


# ── Significance scoring ──────────────────────────────────────────────────────


def significance_score(text: str) -> float:
    """Heuristic significance in [0, 1]. Higher = more worth remembering."""
    stripped = text.strip()
    if not stripped:
        return 0.0

    score = 0.25  # base
    for pat in _POSITIVE_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE):
            score += 0.12
    for pat in _NEGATIVE_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE):
            score -= 0.35
    # Length bonus: a substantive sentence beats a fragment, but cap it.
    score += min(len(stripped) / 600.0, 0.2)
    return max(0.0, min(1.0, score))


# ── Dedup (risk R5) ───────────────────────────────────────────────────────────


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}


def jaccard_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity in [0, 1] — cheap, dependency-free dedup."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# ── Distillation ──────────────────────────────────────────────────────────────


def distill(raw: str, config: MemoryUploadConfig | None = None) -> list[MemoryPoint]:
    """Split free text into candidate points and keep only the significant ones.

    Splits on blank lines and sentence boundaries, scores each candidate, filters
    by ``min_score``/length, dedups against the *other* candidates, and caps the
    result at ``max_points_per_upload`` (highest-significance first).
    """
    cfg = config or MemoryUploadConfig()
    # Split on blank lines first (paragraphs), then over-long paragraphs by sentence.
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", raw):
        para = para.strip()
        if not para:
            continue
        if len(para) <= cfg.max_chars:
            chunks.append(para)
        else:
            chunks.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip())

    candidates: list[MemoryPoint] = []
    for chunk in chunks:
        if not (cfg.min_chars <= len(chunk) <= cfg.max_chars):
            continue
        score = significance_score(chunk)
        if score < cfg.min_score:
            continue
        # intra-batch dedup
        if any(jaccard_similarity(chunk, c.text) >= cfg.dedup_threshold for c in candidates):
            continue
        candidates.append(MemoryPoint(text=chunk, significance=score))

    candidates.sort(key=lambda p: p.significance, reverse=True)
    return candidates[: cfg.max_points_per_upload]


# ── Uploader ──────────────────────────────────────────────────────────────────


class MemoryUploader:
    """Orchestrates distil → redact → dedup → write → (best-effort) ingest."""

    def __init__(self, config: MemoryUploadConfig | None = None) -> None:
        self.config = config or MemoryUploadConfig()

    def _corpus_dir(self) -> Path:
        if self.config.corpus_dir is not None:
            return self.config.corpus_dir
        try:
            from beagle.config.paths import get_memory_dir

            return get_memory_dir() / "uploads"
        except ImportError:
            return Path.home() / ".beagle" / "memory" / "uploads"

    def _existing_corpus_texts(self, corpus_dir: Path) -> list[str]:
        texts: list[str] = []
        if not corpus_dir.exists():
            return texts
        for md in corpus_dir.glob("*.md"):
            try:
                body = md.read_text(encoding="utf-8")
                # Corpus files wrap the raw memory point in provenance metadata.
                # For dedup we compare the actual memory text, not the headers.
                if "\n\n" in body:
                    _, _, memory_text = body.rpartition("\n\n")
                    texts.append(memory_text.strip())
                else:
                    texts.append(body)
            except OSError as exc:
                logger.warning(
                    "Cannot read corpus file %s (%s); excluding it from the dedup set, "
                    "so a duplicate memory may be uploaded.",
                    md,
                    exc,
                )
                continue
        return texts

    def remember(self, raw_text: str, source: str = "session") -> UploadResult:
        """One-shot entry point: distil ``raw_text`` and upload what survives the gates."""
        result = UploadResult()
        points = distill(raw_text, self.config)
        if not points:
            result.rejected.append((raw_text[:80], "no candidate passed significance gate"))
            return result

        corpus_dir = self._corpus_dir()
        existing = self._existing_corpus_texts(corpus_dir) if self.config.dedup_enabled else []

        accepted: list[MemoryPoint] = []
        for point in points:
            point.source = source

            # secrets gate (R4) — non-negotiable
            scrubbed, still_matches = redact(point.text)
            if still_matches:
                result.rejected.append((point.text[:80], "secret survived scrub — dropped"))
                continue
            # If scrubbing removed all substantive content, the point is not
            # worth remembering (e.g. a bare PEM marker reduced to [REDACTED]).
            stripped = scrubbed.strip()
            if stripped == "[REDACTED]" or not stripped:
                result.rejected.append(
                    (point.text[:80], "secret-only content dropped after redaction")
                )
                continue
            point.scrubbed = scrubbed != point.text
            point.text = scrubbed

            # dedup against existing corpus (R5)
            if existing and any(
                jaccard_similarity(point.text, e) >= self.config.dedup_threshold for e in existing
            ):
                result.rejected.append((point.text[:80], "duplicate of existing memory"))
                continue

            accepted.append(point)

        if not accepted:
            return result

        # Write durable markdown corpus files first (source of truth).
        corpus_dir.mkdir(parents=True, exist_ok=True)
        for point in accepted:
            path = corpus_dir / f"mem_{point.point_id}.md"
            try:
                path.write_text(point.to_markdown(self.config.workstream), encoding="utf-8")
                result.uploaded.append(point)
                result.corpus_paths.append(str(path))
            except OSError as exc:
                result.rejected.append((point.text[:80], f"corpus write failed: {exc}"))

        if not result.uploaded:
            return result

        # Best-effort: persist to the hierarchical long-term store.
        result.memory_stored = self._store_hierarchical(result.uploaded)
        # Best-effort: ingest the corpus dir into inline RAG.
        result.rag_ingested = self._ingest_rag(corpus_dir)

        logger.info(
            "[MemoryUpload] uploaded=%d rejected=%d rag_ingested=%s memory_stored=%d",
            len(result.uploaded),
            len(result.rejected),
            result.rag_ingested,
            result.memory_stored,
        )
        return result

    def _store_hierarchical(self, points: list[MemoryPoint]) -> int:
        """Best-effort store into HierarchicalMemory LONG_TERM. Returns count stored."""
        try:
            import asyncio

            from beagle.memory.hierarchical_memory import (
                MemoryLevel,
                get_hierarchical_memory,
            )

            async def _run() -> int:
                mem = await get_hierarchical_memory()
                n = 0
                for p in points:
                    await mem.store(
                        p.text,
                        MemoryLevel.LONG_TERM,
                        metadata={
                            "source": p.source,
                            "significance": p.significance,
                            "workstream": self.config.workstream,
                            "scrubbed": p.scrubbed,
                            "uploaded_at_utc": datetime.now(UTC).isoformat(),
                        },
                    )
                    n += 1
                return n

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_run())
            # Already inside a loop (e.g. orchestrator) — skip the sync store;
            # the corpus file remains the durable artefact.
            logger.debug("[MemoryUpload] inside running loop; deferring hierarchical store")
            return 0
        except ImportError:
            return 0
        except (RuntimeError, OSError, ValueError) as exc:
            logger.warning("[MemoryUpload] hierarchical store skipped: %s", exc)
            return 0

    def _ingest_rag(self, corpus_dir: Path) -> bool:
        """Best-effort inline-RAG ingest of the corpus dir. Returns success flag."""
        try:
            from beagle.infrastructure import cast_ingestion

            cast_ingestion.ingest(corpus_dir)
            return True
        except ImportError:
            return False
        except (RuntimeError, OSError, ValueError) as exc:
            logger.warning("[MemoryUpload] RAG ingest skipped: %s", exc)
            return False


def remember(raw_text: str, source: str = "session") -> UploadResult:
    """Module-level convenience wrapper around :class:`MemoryUploader`."""
    return MemoryUploader().remember(raw_text, source=source)
