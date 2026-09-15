"""Setu Recon Engine - configuration.

Reconciled design note:
- sarvam-30b is deprecated/removed. sarvam-105b is the sole LLM.
- All deterministic work (matching, arithmetic, classification) is Tier-A code.
- The LLM is used ONLY for judgement: ambiguous classification rationale,
  plain-English explanations, and JV narrations.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import os


def _load_dotenv(path: str | Path | None = None) -> None:
    """Tiny zero-dependency .env loader.

    Reads KEY=VALUE pairs from the project-root .env (or an explicit path)
    into the process environment. Real shell env vars take precedence so an
    exported SARVAM_API_KEY always wins over the .env copy.
    """
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# Load .env at import time so every Settings default (SARVAM_API_KEY,
# SARVAM_BASE_URL, SARVAM_MODEL, ...) is picked up by any entry point:
# web API, agent CLI, demo runner, tests, report generation.
_load_dotenv()


@dataclass(frozen=True)
class MatchRules:
    """Configurable per-client match tolerance (W2 - configurable keys/tolerance).

    Values here are the v1 defaults from the architecture HTML: Levenshtein <= 2,
    date window +-7 days, taxable value tolerance +-1%.
    """
    invoice_edit_max: int = 2
    date_window_days: int = 7
    value_tolerance: float = 0.01  # 1%
    require_gstin_exact: bool = True
    # Phase-2: 2A materiality gate (firm-wide default; per-client override).
    # None = no gate. Exceptions with taxable_value >= this are 'above' and
    # require individual review (never batch-approved) per PRD M2 locked #3.
    materiality_amount: Decimal | None = None

    @classmethod
    def from_client_config(cls, cfg: dict | None) -> "MatchRules":
        """Load match rules from a client_config dict (JSONB in PostgreSQL)."""
        if not cfg or "match_rules" not in cfg:
            return cls()
        m = cfg["match_rules"]
        ma = m.get("materiality_amount")
        if ma in (None, ""):
            ma = None
        else:
            ma = Decimal(str(ma))
        return cls(
            invoice_edit_max=m.get("invoice_edit_max", cls.invoice_edit_max),
            date_window_days=m.get("date_window_days", cls.date_window_days),
            value_tolerance=m.get("value_tolerance", cls.value_tolerance),
            require_gstin_exact=m.get("require_gstin_exact", cls.require_gstin_exact),
            materiality_amount=ma,
        )


@dataclass(frozen=True)
class Settings:
    """Engine + Sarvam configuration, mirroring the .env layout."""

    # Sarvam API
    sarvan_api_key: str = field(default_factory=lambda: os.getenv("SARVAM_API_KEY", ""))
    sarvan_base_url: str = field(default_factory=lambda: os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai"))
    model: str = field(default_factory=lambda: os.getenv("SARVAM_MODEL", "sarvam-105b"))
    # Sarvam-105B has thinking mode ON by default and reasoning counts against
    # max_tokens; too small a budget and the reply comes back empty (reasoning
    # eats everything). 16384 keeps complex prompts producing real content.
    sarvan_max_tokens: int = field(default_factory=lambda: int(os.getenv("SARVAM_MAX_TOKENS", "16384")))

    # Tally
    tally_odbc_dsn: str = field(default_factory=lambda: os.getenv("TALLY_ODBC_DSN", "TallyODBC_9000"))

    # Engine behaviour
    llm_enabled: bool = field(default_factory=lambda: os.getenv("SETU_LLM_ENABLED", "1") == "1")
    drafter_verifier: bool = field(default_factory=lambda: os.getenv("SETU_DRAFTER_VERIFIER", "1") == "1")

    @property
    def has_api_key(self) -> bool:
        return bool(self.sarvan_api_key)
