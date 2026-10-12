"""Phase-3 direction (sales vs purchase) and client name aliases.

THE QUESTION
------------
Is this document something we SOLD (sales) or something we BOUGHT (purchase)?
The answer decides which register the document belongs in, so a wrong answer
quietly corrupts a return. This module therefore errs towards asking:

* a clean GSTIN match is DECIDED (``sales`` / ``purchase``),
* a name near-match is only a HINT (``likely_purchase``) that a person confirms,
* anything else is ``undetermined``,
* and NOTHING is defaulted. An undetermined document is excluded from every
  register until a person decides.

WHY A PAN IS IGNORED
--------------------
A PAN printed beside the issuer's name is the ISSUER's own PAN — it says nothing
about who the document is addressed to. The only PAN that could decide direction
is one the document LABELS as the recipient's, and the canonical field set does
not read that label, so this module never uses a PAN to decide. (It reads
``recipient_pan`` only when the field was actually captured as the recipient's.)

WHY ACCURACY, NOT RECALL, ON THE NAME MATCH
-------------------------------------------
Matching the recipient's name to the client is the only signal left once the
GSTINs do not match, so it is worth doing — but a wrong "likely_purchase" is
worse than an "undetermined", because the former invites a one-click
confirmation. The match is therefore conservative, always reports WHAT
differed, and the client's name core must agree in full.

LEGAL FORM MATTERS
------------------
``KMAP Capital Pvt Ltd`` and ``KMAP Capital Limited`` are the SAME client
spelled two ways, but they are NOT the same string, and the difference is
exactly the part a registration is tied to. The match therefore reports
``exact`` only when the names agree INCLUDING their legal form, and ``near``
(with the difference spelled out) when they agree only after it is stripped.
Both are close enough for ``likely_purchase``; only the reviewer's confirmation
makes it ``purchase``.

ALIASES ARE NEVER AUTOMATIC
---------------------------
An alias is a row in ``client_aliases`` created ONLY by a reviewer ticking
"remember this name" on a document they have just confirmed (``remember_alias``).
It is revocable (``revoke_alias``), it is never created from an individual's
name, and no code path creates one from a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from src.invoice_extract.doc_context import DocContext
from src.invoice_extract.rules_schema import (
    DIRECTION_LIKELY_PURCHASE,
    DIRECTION_LIKELY_SALES,
    DIRECTION_PURCHASE,
    DIRECTION_SALES,
    DIRECTION_UNDETERMINED,
)

#: A name match this close (or closer) is worth showing as a hint.
NAME_MATCH_THRESHOLD = 0.85

_PUNCT_RE = re.compile(r"[^0-9a-z\s]")


def normalise_party_name(text: Optional[str]) -> str:
    """Fold a party name for comparison.

    Casefolds, replaces every non-alphanumeric character with a space, and
    collapses whitespace. Punctuation is REMOVED rather than kept: an invoice
    prints ``M/s. KMAP Capital`` and a register holds ``KMAP Capital``, and the
    difference is not evidence of two different parties.
    """
    if text is None:
        return ""
    folded = _PUNCT_RE.sub(" ", str(text).casefold())
    return " ".join(folded.split())


def split_legal_form(text: Optional[str], suffixes: tuple[str, ...]) -> tuple[str, str]:
    """(core name, the legal-form words removed) for one party name."""
    words = normalise_party_name(text).split()
    form: list[str] = []
    while words:
        last = words[-1]
        if last in suffixes or (last in ("and", "&", "co") and len(form) >= 1):
            form.insert(0, words.pop())
            continue
        break
    # "pvt ltd" / "private limited" strip as one unit; anything left that is a
    # suffix word is still part of the form.
    return " ".join(words), " ".join(form)


@dataclass(frozen=True)
class NameMatch:
    """How closely a printed party name matches the client."""

    kind: str                     # exact | near | none
    similarity: float
    core: str
    difference: str
    matched_text: str
    against: str                  # 'legal name' | 'alias: …'
    stripped_form: str

    @property
    def is_match(self) -> bool:
        return self.kind in ("exact", "near")

    def explanation(self) -> str:
        if self.kind == "exact":
            return f"the recipient name is the client's {self.against} ({self.matched_text})"
        if self.kind == "near":
            if self.difference:
                return (
                    f"the recipient name {self.matched_text!r} matches the client's {self.against} "
                    f"{self.core!r} — {self.difference}"
                )
            return f"the recipient name {self.matched_text!r} closely matches the client's {self.against}"
        return ""


def match_client_name(
    text: Optional[str], *, legal_name: Optional[str], aliases: tuple[dict[str, Any], ...],
    suffixes: tuple[str, ...],
) -> NameMatch:
    """Match one printed name against the client's legal name and its aliases."""
    import difflib

    candidate = normalise_party_name(text)
    if not candidate:
        return NameMatch("none", 0.0, "", "no name is printed", "", "", "")
    candidate_core, candidate_form = split_legal_form(text, suffixes)

    best = NameMatch("none", 0.0, "", "", "", "", "")
    for against_label, against_raw in [("legal name", legal_name)] + [
        (f"alias {a.get('alias_text')!r}", a.get("alias_text")) for a in aliases
    ]:
        if not against_raw:
            continue
        against = normalise_party_name(against_raw)
        if not against:
            continue
        against_core, against_form = split_legal_form(against_raw, suffixes)
        if candidate == against:
            return NameMatch(
                "exact", 1.0, candidate_core, "", str(text).strip(), against_label, "",
            )
        # Compare the cores: agreement in full after the legal form is removed.
        core_similarity = difflib.SequenceMatcher(None, candidate_core, against_core).ratio()
        full_similarity = difflib.SequenceMatcher(None, candidate, against).ratio()
        if candidate_core and candidate_core == against_core:
            difference = (
                f"the legal form differs: {candidate_form or 'none'} vs {against_form or 'none'}"
            )
            return NameMatch(
                "near", max(full_similarity, core_similarity), against_core, difference,
                str(text).strip(), against_label, candidate_form or against_form,
            )
        if full_similarity > best.similarity:
            best = NameMatch(
                "near" if full_similarity >= NAME_MATCH_THRESHOLD else "none",
                full_similarity, against_core,
                "the names differ in wording", str(text).strip(), against_label,
                candidate_form,
            )
    return best


def looks_like_an_individual(
    text: Optional[str], *, stopwords: frozenset[str], suffixes: tuple[str, ...] = (),
) -> bool:
    """Is this a person's name rather than a business?

    Used ONLY to decide whether a recipient can support a direction hint or an
    alias — never for matching. Deliberately conservative: a name is treated as
    a person when it has NO legal form, no business word, no digits, and looks
    like 1-4 capitalised words. A business name we fail to recognise is simply
    not called an individual, and the document stays undetermined.

    The legal-form test is a hard signal in its own right, independent of the
    configurable stopword list: if stripping a legal form actually removed
    something, the name belongs to a registered entity, not a person.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if any(ch.isdigit() for ch in raw):
        return False
    if suffixes and split_legal_form(raw, suffixes)[1]:
        return False
    words = normalise_party_name(raw).split()
    if not (1 < len(words) <= 4):
        return False
    if any(word in stopwords for word in words):
        return False
    return True


@dataclass(frozen=True)
class DirectionResult:
    direction: str
    basis: str
    match: dict[str, Any]
    scope: Optional[str] = None

    @property
    def needs_a_human(self) -> bool:
        return self.direction in (DIRECTION_LIKELY_SALES, DIRECTION_LIKELY_PURCHASE, DIRECTION_UNDETERMINED)


def decide(ctx: DocContext) -> DirectionResult:
    """Decide the direction from the client's own identifiers and name."""
    client = ctx.client or {}
    client_gstin = str(client.get("gstin") or "").strip().upper()
    issuer = str(ctx.value("issuer_gstin") or "").strip().upper()
    recipient = str(ctx.value("recipient_gstin") or "").strip().upper()
    match: dict[str, Any] = {
        "client_id": ctx.client_id,
        "client_gstin": client_gstin or None,
        "issuer_gstin": issuer or None,
        "recipient_gstin": recipient or None,
    }

    if not client_gstin:
        # Never guess from a PAN or a name when the client has no registration:
        # every document for this client needs a person.
        return DirectionResult(
            DIRECTION_UNDETERMINED,
            "the client has no GSTIN on its record, so no GSTIN match is possible",
            match,
        )

    issuer_hit = bool(issuer) and issuer == client_gstin
    recipient_hit = bool(recipient) and recipient == client_gstin
    if issuer_hit and recipient_hit:
        match["conflict"] = "both GSTINs are the client's"
        return DirectionResult(
            DIRECTION_UNDETERMINED,
            "both the issuer and the recipient GSTIN are the client's own — a self-invoice or a data problem, "
            "so this needs a person",
            match,
        )
    if issuer_hit:
        return DirectionResult(
            DIRECTION_SALES, f"the issuer GSTIN is the client's ({client_gstin})", match,
        )
    if recipient_hit:
        return DirectionResult(
            DIRECTION_PURCHASE, f"the recipient GSTIN is the client's ({client_gstin})", match,
        )

    # ---- no GSTIN match: try the recipient's NAME
    recipient_name = ctx.value("recipient_name")
    if not ctx.has_value("recipient_name"):
        return DirectionResult(
            DIRECTION_UNDETERMINED,
            "no GSTIN matched the client and no recipient name is printed",
            match,
        )
    if looks_like_an_individual(
        recipient_name, stopwords=ctx.config.individual_stopwords,
        suffixes=ctx.config.legal_form_suffixes,
    ):
        match["recipient_looks_like_an_individual"] = True
        return DirectionResult(
            DIRECTION_UNDETERMINED,
            f"the recipient {recipient_name!r} looks like an individual, not the client",
            match,
        )

    name_match = match_client_name(
        recipient_name, legal_name=client.get("legal_name"),
        aliases=ctx.aliases, suffixes=ctx.config.legal_form_suffixes,
    )
    match["name_match"] = {
        "kind": name_match.kind, "similarity": round(name_match.similarity, 4),
        "core": name_match.core, "difference": name_match.difference,
        "matched_text": name_match.matched_text, "against": name_match.against,
    }
    if name_match.is_match:
        return DirectionResult(
            DIRECTION_LIKELY_PURCHASE,
            f"the recipient name matches the client — {name_match.explanation()}",
            match,
        )
    return DirectionResult(
        DIRECTION_UNDETERMINED,
        f"no GSTIN matched the client and the recipient {recipient_name!r} is not the client's name",
        match,
    )


def can_be_remembered_as_alias(
    ctx: DocContext, alias_text: Optional[str],
) -> tuple[bool, str]:
    """May this name be remembered as an alias of the client?

    Refused for an individual's name (a person's name is not an identifier of
    the client's business) and for anything that is not a name at all.
    """
    text = str(alias_text or "").strip()
    if not text:
        return False, "there is no name to remember"
    if looks_like_an_individual(
        text, stopwords=ctx.config.individual_stopwords, suffixes=ctx.config.legal_form_suffixes,
    ):
        return False, (
            f"{text!r} looks like an individual's name — an alias is for the client's own trading names, "
            "never a person"
        )
    return True, ""
