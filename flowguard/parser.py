"""
flowguard/nlp/parser.py
────────────────────────
NLP LAYER — human text → ScoreRequest  AND  EngineResult → human text.

RECOMMENDED OPEN-SOURCE NLP STACK:
───────────────────────────────────
• spaCy (en_core_web_sm / xx_ent_wiki_sm for multilingual)
    - Named Entity Recognition: ORG, MONEY, DATE, GPE
    - Rule-based Matcher for Indian number formats (₹1.5L, 5 lakh, 20k)
    - Fast, runs locally, no API key needed
    - pip install spacy && python -m spacy download en_core_web_sm

• sentence-transformers (all-MiniLM-L6-v2 — 80 MB, best speed/accuracy)
    - Used for intent classification (zero-shot via cosine similarity)
    - pip install sentence-transformers

• IndicNLP (for Tamil / Hindi / Telugu)
    - pip install indic-nlp-library

Why NOT alternatives:
  • NLTK         — no NER quality, dated
  • BERT-base    — too large for a hackathon server, spaCy is faster
  • GPT-4 for parsing — defeats the "deterministic" contract
  • Rasa         — overkill for extraction-only task

For WhatsApp specifically, user messages are short (< 200 chars),
so the parser is designed for telegraphic Indian-English:
  "gst 20k due friday, rent 25000 due 5th, cash 1 lakh"
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .database import compute_obligation_id

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CONFIG LOADER
# ─────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

def _load_parser_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

_CFG = _load_parser_config()
_PARSER_CFG = _CFG.get("parser", {})
_LEGAL_DISCLAIMER = _CFG.get(
    "legal_disclaimer",
    "\u2696\ufe0f FlowGuard is a decision-support tool, not financial or legal advice. "
    "Always consult a qualified CA or financial advisor before acting on these recommendations."
)


# ─────────────────────────────────────────────
# OPTIONAL: lazy-load spaCy so the file imports
# even without the model downloaded
# ─────────────────────────────────────────────
try:
    import spacy
    _NLP = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except Exception:
    _NLP = None
    SPACY_AVAILABLE = False
    logger.warning(
        "spaCy model not found.  Falling back to regex parser.  "
        "Install with: pip install spacy && python -m spacy download en_core_web_sm"
    )

# Optional sentence-transformers for intent classification
try:
    from sentence_transformers import SentenceTransformer, util as st_util
    _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
    ST_AVAILABLE = True
except Exception:
    _EMBEDDER = None
    ST_AVAILABLE = False
    logger.warning(
        "sentence-transformers not found.  Using keyword intent matching.  "
        "Install with: pip install sentence-transformers"
    )


# ─────────────────────────────────────────────
# INTENT LABELS & EXAMPLES
# used for zero-shot classification via sentence embeddings
# ─────────────────────────────────────────────

INTENT_EXAMPLES: dict[str, list[str]] = {
    "score_obligations": [
        "I have GST of 20000 due on Friday and rent of 25000 due next week",
        "Pay my supplier 80k by Thursday, my cash is 1 lakh",
        "gst 20k friday, rent 25k monday, cash 60k",
        "Tell me which bills to pay first",
    ],
    "what_if": [
        "What if Kapoor pays me 30000 tomorrow?",
        "If I get 50k from Sharma on Wednesday what changes?",
        "Suppose I receive 20 lakh next Monday",
    ],
    "days_to_zero": [
        "How many days before I run out of money?",
        "When will my cash hit zero?",
        "How long can I survive without new income?",
    ],
    "draft_email": [
        "Draft a delay email to my supplier",
        "Write a message to my landlord explaining the delay",
        "Help me tell Kapoor I need more time",
    ],
    "status": [
        "What's my current situation?",
        "Show me the summary",
        "What should I do today?",
    ],
}


# ─────────────────────────────────────────────
# INDIAN NUMBER FORMAT HELPERS
# ─────────────────────────────────────────────
# Handles: ₹1.5L, 1 lakh, 20k, 20,000, ₹20000, 1.5 crore

_AMOUNT_PATTERNS = [
    (r"(?:₹|rs\.?\s*|inr\s*)?([\d,]+(?:\.\d+)?)\s*(?:cr|crore|crores)\b", lambda m: round(float(m.group(1).replace(",", "")) * 1e7, 2)),
    (r"(?:₹|rs\.?\s*|inr\s*)?([\d,]+(?:\.\d+)?)\s*(?:l|lakh|lakhs|lac|lacs)\b",  lambda m: round(float(m.group(1).replace(",", "")) * 1e5, 2)),
    (r"(?:₹|rs\.?\s*|inr\s*)?([\d,]+(?:\.\d+)?)\s*k\b",             lambda m: round(float(m.group(1).replace(",", "")) * 1e3, 2)),
    (r"(?:₹|rs\.?\s*|inr\s*)([\d,]+(?:\.\d+)?)",                    lambda m: round(float(m.group(1).replace(",", "")), 2)),
    (r"\b([\d,]{4,}(?:\.\d+)?)\b",                                   lambda m: round(float(m.group(1).replace(",", "")), 2)),
]

def extract_amounts(text: str) -> list[float]:
    """Extract all INR amounts from text, in order of appearance.
    Tracks match positions to prevent double-counting from overlapping patterns.
    """
    results: list[tuple[int, int, float]] = []  # (start, end, value)
    text_lower = text.lower()
    for pattern, converter in _AMOUNT_PATTERNS:
        for m in re.finditer(pattern, text_lower):
            try:
                val = converter(m)
                results.append((m.start(), m.end(), val))
            except Exception:
                pass

    # Reject overlapping matches: keep the match that starts earliest;
    # among overlaps, prefer the pattern that matched first (higher priority).
    results.sort(key=lambda x: (x[0], -x[1]))  # sort by start, then longest
    accepted: list[tuple[int, int, float]] = []
    for start, end, val in results:
        if any(start < prev_end and end > prev_start
               for prev_start, prev_end, _ in accepted):
            continue  # overlaps with an already-accepted match
        accepted.append((start, end, val))

    # Return all amounts in position order (overlap already rejected above)
    return [v for _, _, v in accepted]


# ─────────────────────────────────────────────
# DATE PARSING (Indian context)
# ─────────────────────────────────────────────

_DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_RELATIVE_MAP = {
    "today": 0, "tomorrow": 1, "day after": 2,
    "end of month": None,  # special handling
    "next week": 7, "next month": 30,
    "eom": None,
}
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def parse_date(text: str, reference: Optional[date] = None) -> Optional[date]:
    """
    Parse a date expression from natural language.
    reference defaults to today.

    Handles:
      "friday", "this friday", "next monday"
      "5th", "20th", "2nd"  (day of current/next month)
      "15 march", "march 15"
      "today", "tomorrow", "end of month"
      "2025-03-26", "26/03/2025"
    """
    ref = reference or date.today()
    txt = text.lower().strip()

    # ISO / numeric dates
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            pass

    # Relative keywords
    for kw, offset in _RELATIVE_MAP.items():
        if kw in txt:
            if offset is None:
                # end of month
                next_month = ref.replace(day=28) + timedelta(days=4)
                return next_month.replace(day=1) - timedelta(days=1)
            return ref + timedelta(days=offset)

    # Weekday names (ensure word boundaries so 'mon' does not match 'month' or 'money')
    for name, wd in _DAY_MAP.items():
        if re.search(r"\b" + name + r"\b", txt):
            days_ahead = wd - ref.weekday()
            if f"next {name}" in txt:
                days_ahead = days_ahead + 7 if days_ahead > 0 else days_ahead + 7
            elif days_ahead < 0:
                days_ahead += 7
            return ref + timedelta(days=days_ahead)

    # Ordinal day: "5th", "20th" — suffix is REQUIRED to avoid matching bare digits
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", txt)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                candidate = ref.replace(day=day)
                if candidate < ref:
                    # Next month
                    nxt = (ref.replace(day=28) + timedelta(days=4)).replace(day=day)
                    return nxt
                return candidate
            except ValueError:
                pass

    # "15 march" or "march 15"
    for mon_name, mon_num in _MONTH_MAP.items():
        if mon_name in txt:
            m2 = re.search(r"\b(\d{1,2})\b", txt)
            day = int(m2.group(1)) if m2 else 1
            year = ref.year if mon_num >= ref.month else ref.year + 1
            try:
                return date(year, mon_num, day)
            except ValueError:
                pass

    return None


# ─────────────────────────────────────────────
# CATEGORY / FLEXIBILITY INFERENCE
# ─────────────────────────────────────────────

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "STATUTORY":     ["gst", "tds", "pf", "esic", "esi", "tax", "income tax",
                       "provident fund", "professional tax", "pt", "advance tax",
                       "service tax", "custom duty", "excise"],
    "SECURED_LOAN":  ["emi", "loan", "bank", "nbfc", "mortgage", "term loan",
                       "credit", "overdraft", "od", "cc limit"],
    "SALARY":        ["salary", "salaries", "wages", "wage", "payroll", "staff",
                       "employee", "workers"],
    "RENT":          ["rent", "lease", "landlord", "premises", "office rent",
                       "shop rent", "godown"],
    "UTILITY":       ["electricity", "power", "water", "internet", "wifi",
                       "broadband", "phone", "mobile bill", "utility"],
    "TRADE_PAYABLE": ["supplier", "vendor", "invoice", "payment", "bill",
                       "purchase", "material", "raw material", "goods", "trader", "supplies"],
}

_FLEXIBILITY_KEYWORDS: dict[str, list[str]] = {
    "FIXED":       ["gst", "tds", "emi", "loan", "salary", "wages", "court",
                     "statutory", "legal"],
    "NEGOTIABLE":  ["supplier", "vendor", "landlord", "rent"],
    "DEFERRABLE":  ["misc", "other", "petty", "discretionary"],
}


def infer_category(description: str) -> str:
    desc = description.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in desc for kw in keywords):
            return cat
    return "OTHER"


def infer_flexibility(description: str, category: str) -> str:
    desc = description.lower()
    if category in ("STATUTORY", "SECURED_LOAN", "SALARY"):
        return "FIXED"
    for flex, keywords in _FLEXIBILITY_KEYWORDS.items():
        if any(kw in desc for kw in keywords):
            return flex
    return "NEGOTIABLE"  # safe default


def infer_penalty_rate(category: str) -> float:
    """Default annual penalty rates from Indian law / market practice.
    Loaded from config.json if available, else hardcoded fallback.
    """
    _DEFAULT_RATES = {
        "STATUTORY":     18.0,
        "SECURED_LOAN":  24.0,
        "SALARY":         0.0,
        "RENT":          12.0,
        "UTILITY":        0.0,
        "TRADE_PAYABLE":  0.0,
        "OTHER":          0.0,
    }
    rates = _PARSER_CFG.get("default_penalty_rates", _DEFAULT_RATES)
    return rates.get(category, 0.0)


# ─────────────────────────────────────────────
# INTENT CLASSIFIER
# ─────────────────────────────────────────────

def classify_intent(text: str) -> str:
    """
    Returns one of: score_obligations, what_if, days_to_zero,
                    draft_email, status.

    Uses sentence-transformers if available, else keyword fallback.
    """
    if ST_AVAILABLE and _EMBEDDER is not None:
        q_emb = _EMBEDDER.encode(text, convert_to_tensor=True)
        best_intent, best_score = "score_obligations", -1.0
        for intent, examples in INTENT_EXAMPLES.items():
            e_emb = _EMBEDDER.encode(examples, convert_to_tensor=True)
            sim = float(st_util.pytorch_cos_sim(q_emb, e_emb).max())
            if sim > best_score:
                best_score = sim
                best_intent = intent
        return best_intent

    # Keyword fallback
    txt = text.lower()
    if any(w in txt for w in ["what if", "suppose", "if i get", "if kapoor"]):
        return "what_if"
    if any(w in txt for w in ["days", "run out", "zero", "survive"]):
        return "days_to_zero"
    if any(w in txt for w in ["email", "message", "write", "draft", "letter"]):
        return "draft_email"
    if any(w in txt for w in ["summary", "situation", "status", "show me"]):
        return "status"
    return "score_obligations"


# ─────────────────────────────────────────────
# CORE PARSER:  text → list[Obligation] + CashPosition
# ─────────────────────────────────────────────

def _extract_entities_spacy(text: str) -> dict:
    """Use spaCy NER to extract MONEY, DATE, ORG entities."""
    if not SPACY_AVAILABLE or _NLP is None:
        return {"orgs": [], "dates": [], "money": []}
    doc = _NLP(text)
    return {
        "orgs":  [ent.text for ent in doc.ents if ent.label_ == "ORG"],
        "dates": [ent.text for ent in doc.ents if ent.label_ == "DATE"],
        "money": [ent.text for ent in doc.ents if ent.label_ == "MONEY"],
    }


def parse_text_to_obligations(
    raw_text: str,
    reference_date: Optional[date] = None,
) -> tuple[list[dict], float]:
    """
    Parse free-form human text into (obligations_dicts, cash_inr).

    Returns raw dicts (not Obligation models) so the API layer can
    validate them with Pydantic and return proper 422 errors.

    Strategy (in priority order):
      1. Structured segments split by comma / semicolon / newline
      2. spaCy NER for ORG + MONEY + DATE alignment
      3. Regex fallback for amounts and dates
      4. Keyword heuristics for category / flexibility

    Example inputs this handles:
      "GST 20k due friday, rent 25000 due 5th, cash 1 lakh"
      "I owe my supplier 80000 by Thursday. My bank balance is 60000."
      "salary 45k due end of month, EMI 12500 due 3rd"
    """
    ref = reference_date or date.today()
    text_lower = raw_text.lower()

    # ── Detect cash balance ──────────────────
    cash_inr = 0.0
    cash_patterns = [
        r"(?:cash|balance|bank|available|have|got)\s+(?:is\s+|of\s+|:?\s*)?([\d,]+(?:\.\d+)?)\s*(?:cr|crore|l|lakh|lac|k)?",
        r"([\d,]+(?:\.\d+)?)\s*(?:cr|crore|l|lakh|lac|k)?\s+(?:in\s+)?(?:cash|bank|hand|balance)",
    ]
    for cp in cash_patterns:
        m = re.search(cp, text_lower)
        if m:
            amounts = extract_amounts(m.group(0))
            if amounts:
                cash_inr = amounts[0]
                break

    _PRIMARY_SPLIT = re.compile(
        r",(?!\d)|;\s*|\n|\.\s+|"
        r"\bwhile\b|"
        r"\balso\b|"
        r"(?<!\d)\band\b",
        re.IGNORECASE,
    )
    _COMPACT_OBL_KW = re.compile(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"today|tomorrow|month|eom|l|lakh|lac|crore|k|\d+"
        r")\s+(?:(?:for|to|of|towards)\s+)?"
        r"(gst|tds|rent|salary|wages|emi|loan|supplier|suppliers|vendor|invoice|"
        r"utility|electricity|water|internet|bill|payment|supplies|purchase)\b",
        re.IGNORECASE,
    )
    _AMOUNT_TO_PARTY = re.compile(
        r"(\d+(?:\.\d+)?\s*(?:l|lakh|lac|k|cr|crore)?)"
        r"\s+(?:to|for)\s+"
        r"(\w+)"
        r"\s+(?:within|by|before|due)\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"today|tomorrow|\d{1,2}(?:st|nd|rd|th))",
        re.IGNORECASE,
    )

    segments_raw = _PRIMARY_SPLIT.split(raw_text)
    refined: list[str] = []
    for seg in segments_raw:
        if not seg:
            continue
        # Inject " ||| " between the date/number and the next obligation keyword
        marked_seg = _COMPACT_OBL_KW.sub(r"\1 ||| \2", seg)
        parts = marked_seg.split(" ||| ")
        refined.extend(parts)
    segments = [s.strip() for s in refined if s.strip()]

    obligations: list[dict] = []
    spacy_data = _extract_entities_spacy(raw_text)

    for seg in segments:
        seg_lower = seg.lower()

        # Skip segments that are purely about cash
        if re.search(r"\b(?:cash|balance|bank|available|have|got)\b", seg_lower) and not re.search(r"\b(?:due|pay|owe|supplier|gst|rent|salary|emi)\b", seg_lower):
            continue

        # ── Extract amount ───────────────────
        amounts = extract_amounts(seg)
        if not amounts:
            continue
        amount = amounts[0]

        # ── Extract due date ─────────────────
        due_date = None
        # Look for "due <date>" or "by <date>"
        due_m = re.search(r"(?:due|by|on|before)\s+(.+?)(?:,|$|\.|and)", seg_lower)
        if due_m:
            due_date = parse_date(due_m.group(1).strip(), ref)
        if due_date is None:
            # Try parsing any date expression in the segment
            due_date = parse_date(seg_lower, ref)
        if due_date is None:
            due_date = ref + timedelta(days=7)  # default: 1 week

        # ── Infer counterparty name ──────────
        counterparty = "Unknown"
        # Look for "to <name>" or "from <name>"
        cp_m = re.search(r"(?:to|from|owed to|pay to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", seg)
        if cp_m:
            counterparty = cp_m.group(1)
        else:
            # Use ORG entities from spaCy
            for org in spacy_data["orgs"]:
                if org.lower() in seg_lower:
                    counterparty = org
                    break

        # ── Infer category ───────────────────
        category = infer_category(seg_lower)

        # If no category matched but we have a counterparty name, default trade payable
        if category == "OTHER" and counterparty != "Unknown":
            category = "TRADE_PAYABLE"

        # ── Use category to set counterparty for unnamed statutory ───
        if counterparty == "Unknown":
            friendly_names = {
                "STATUTORY":     "Government / Tax Authority",
                "SECURED_LOAN":  "Bank / Lender",
                "SALARY":        "Employees",
                "RENT":          "Landlord",
                "UTILITY":       "Utility Provider",
                "TRADE_PAYABLE": "Supplier",
                "OTHER":         "Other Party",
            }
            counterparty = friendly_names.get(category, "Unknown")

        # ── Infer flexibility ────────────────
        flexibility = infer_flexibility(seg_lower, category)

        # ── Infer penalty rate ───────────────
        penalty_rate = infer_penalty_rate(category)

        # ── Max deferral days ────────────────
        days_left = (due_date - ref).days
        max_deferral_days_map = {
            "STATUTORY":      0 if days_left <= 0 else max(0, days_left - 1),
            "SECURED_LOAN":   0,
            "SALARY":         0 if days_left <= 0 else 2,
            "RENT":           3 if days_left > 3 else 0,
            "UTILITY":        7,
            "TRADE_PAYABLE":  min(14, max(0, days_left + 7)),
            "OTHER":          30,
        }
        max_deferral = max_deferral_days_map.get(category, 7)

        # ── Relationship score ───────────────
        # Default by category; user can override in multi-turn
        relationship_defaults = {
            "STATUTORY":      0,
            "SECURED_LOAN":   50,
            "SALARY":         90,
            "RENT":           60,
            "UTILITY":        30,
            "TRADE_PAYABLE":  50,
            "OTHER":          40,
        }
        relationship_score = relationship_defaults.get(category, 50)

        # ── Compute parse confidence ─────────────
        _pc = 1.0
        if counterparty in ("Unknown", "Other Party", "Supplier"):
            _pc -= 0.20  # counterparty not explicitly found
        if due_date == ref + timedelta(days=7):  # fell back to default
            _pc -= 0.25
        if category == "OTHER":
            _pc -= 0.15  # no keyword matched
        _pc = max(0.0, _pc)

        # ── Build description ────────────────
        description = seg.strip()[:120]

        ob_id = compute_obligation_id(counterparty, amount, due_date)

        obligations.append({
            "obligation_id":          ob_id,
            "counterparty_name":      counterparty,
            "description":            description,
            "amount_inr":             amount,
            "penalty_rate_annual_pct": penalty_rate,
            "due_date":               due_date.isoformat(),
            "max_deferral_days":      max_deferral,
            "category":               category,
            "flexibility":            flexibility,
            "relationship_score":     float(relationship_score),
            "blocks_other_obligation_ids": [],
            "is_recurring":           category in ("SALARY", "RENT", "UTILITY", "SECURED_LOAN"),
            "source_hash":            ob_id,
            "notes":                  None,
            "parse_confidence":       round(_pc, 2),
        })

    return obligations, cash_inr


# ─────────────────────────────────────────────
# WHAT-IF PARAMETER EXTRACTOR
# ─────────────────────────────────────────────

def extract_what_if_params(text: str, reference_date: Optional[date] = None) -> dict:
    """
    Extract parameters from a what-if question.
    Returns: {"inflow_amount": float, "inflow_day_offset": int, "counterparty": str}
    """
    ref = reference_date or date.today()
    amounts = extract_amounts(text)
    inflow_amount = amounts[0] if amounts else 0.0

    # Find the day offset
    inflow_date = parse_date(text, ref)
    day_offset = (inflow_date - ref).days if inflow_date else 1
    day_offset = max(0, day_offset)

    # Counterparty
    cp_m = re.search(r"\b([A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)?)\s+(?:pays?|sends?|gives?)", text, re.IGNORECASE)
    counterparty = cp_m.group(1).title() if cp_m else "Customer"

    return {
        "inflow_amount":    inflow_amount,
        "inflow_day_offset": day_offset,
        "counterparty":     counterparty,
    }


# ─────────────────────────────────────────────
# OUTPUT NARRATOR
# ─────────────────────────────────────────────

from .models import ActionTag, EngineResult, DecisionRecord, ScoreBand


_ACTION_EMOJI = {
    ActionTag.PAY:       "✅",
    ActionTag.DEFER:     "⏳",
    ActionTag.NEGOTIATE: "🤝",
    ActionTag.ESCALATE:  "🚨",
}

_BAND_EMOJI = {
    ScoreBand.CRITICAL: "🔴",
    ScoreBand.HIGH:     "🟠",
    ScoreBand.MEDIUM:   "🟡",
    ScoreBand.LOW:      "🟢",
}


def narrate_result(
    result: EngineResult,
    channel: str = "whatsapp",
    language: str = "en",
) -> str:
    """
    Convert EngineResult → plain human text.

    channel = "whatsapp" : short, emoji-rich, bullet points
    channel = "web"      : full COT, all scores, detailed
    channel = "voice"    : no emoji, flowing sentences (for TTS)
    """
    if channel == "whatsapp":
        text = _narrate_whatsapp(result)
    elif channel == "voice":
        text = _narrate_voice(result)
    else:
        text = _narrate_web(result)

    # Append legal disclaimer to all channels
    if channel == "voice":
        # TTS-friendly version, no emoji
        text += " Please note: FlowGuard is a decision-support tool, not financial or legal advice."
    else:
        text += f"\n\n{_LEGAL_DISCLAIMER}"
    return text


def _fmt_inr(amount: float) -> str:
    """Format INR with lakhs/crore suffixes for readability."""
    if amount >= 1e7:
        return f"₹{amount/1e7:.1f} cr"
    if amount >= 1e5:
        return f"₹{amount/1e5:.1f}L"
    if amount >= 1000:
        return f"₹{amount/1000:.0f}k"
    return f"₹{amount:.0f}"


def _narrate_whatsapp(result: EngineResult) -> str:
    """Under 300 chars for preview; full message stays concise."""
    lines = []

    # Header
    lines.append(f"*FlowGuard Report* — {result.as_of_date.strftime('%d %b %Y')}")
    lines.append(f"💰 Cash: {_fmt_inr(result.available_cash_inr)}  |  Bills: {_fmt_inr(result.total_obligations_inr)}")

    if result.cash_shortfall_inr > 0:
        lines.append(f"⚠️ Shortfall: {_fmt_inr(result.cash_shortfall_inr)}")

    if result.days_to_zero is not None:
        lines.append(f"⏱ *Cash runs out in {result.days_to_zero} day(s)!*")

    lines.append("")
    lines.append("*Priority Actions:*")

    for i, d in enumerate(result.decisions[:5], 1):
        band_e  = _BAND_EMOJI.get(d.score_band, "")
        act_e   = _ACTION_EMOJI.get(d.action, "")
        lines.append(
            f"{i}. {band_e}{act_e} *{d.counterparty_name}* — "
            f"{_fmt_inr(d.amount_inr)} "
            f"(CS {d.consequence_score:.0f})"
        )
        lines.append(f"   _{d.cot_reason}_")

    if len(result.decisions) > 5:
        lines.append(f"...and {len(result.decisions)-5} more. Reply *FULL* for details.")

    # Quick LLM action codes
    lines.append("")
    lines.append("Reply: *EMAIL <name>* to draft a delay email")
    lines.append("Reply: *WHATIF <amount> <when>* for scenario analysis")

    return "\n".join(lines)


def _narrate_web(result: EngineResult) -> str:
    """Full detailed report for web dashboard."""
    sections = []

    sections.append(f"## FlowGuard Analysis — {result.as_of_date.strftime('%d %b %Y')}\n")
    sections.append(
        f"**Available Cash:** {_fmt_inr(result.available_cash_inr)}  |  "
        f"**Total Obligations:** {_fmt_inr(result.total_obligations_inr)}  |  "
        f"**Shortfall:** {_fmt_inr(result.cash_shortfall_inr)}"
    )
    if result.days_to_zero:
        sections.append(f"\n⚠️ **Days to Zero: {result.days_to_zero}** — cash runs out in {result.days_to_zero} days.")

    sections.append("\n---\n### Ranked Decisions\n")

    for d in result.decisions:
        band_e = _BAND_EMOJI.get(d.score_band, "")
        sections.append(f"#### {band_e} {d.counterparty_name} — {_fmt_inr(d.amount_inr)}")
        sections.append(f"- **Score:** {d.consequence_score:.1f} ({d.score_band.value}) | **Action:** {d.action.value} | **Confidence:** {d.confidence*100:.0f}%")
        sections.append(f"- **Why:** {d.cot_reason}")
        sections.append(f"- **Trade-off:** {d.cot_tradeoff}")
        sections.append(f"- **Downstream:** {d.cot_downstream}")
        if d.penalty_per_day_inr > 0:
            sections.append(f"- **Daily penalty if delayed:** {_fmt_inr(d.penalty_per_day_inr)}")
        sections.append("")

    return "\n".join(sections)


def _narrate_voice(result: EngineResult) -> str:
    """TTS-friendly — no markdown, no emoji, flowing sentences."""
    parts = []
    parts.append(
        f"Your FlowGuard report for {result.as_of_date.strftime('%d %B %Y')}. "
        f"You have {_fmt_inr(result.available_cash_inr)} available "
        f"against total obligations of {_fmt_inr(result.total_obligations_inr)}."
    )
    if result.cash_shortfall_inr > 0:
        parts.append(f"You have a shortfall of {_fmt_inr(result.cash_shortfall_inr)}.")
    if result.days_to_zero:
        parts.append(f"Warning: your cash runs out in {result.days_to_zero} days.")

    parts.append("Here are your priority actions.")
    for i, d in enumerate(result.decisions[:3], 1):
        parts.append(
            f"Number {i}: {d.action.value.lower()} {_fmt_inr(d.amount_inr)} "
            f"to {d.counterparty_name}. {d.cot_reason} {d.cot_tradeoff}"
        )

    return " ".join(parts)


def narrate_whatsapp_preview(result: EngineResult) -> str:
    """Under 300 characters — for WhatsApp notification preview."""
    top = result.decisions[0] if result.decisions else None
    if not top:
        return "FlowGuard: No obligations to prioritise."

    prefix = "🚨 " if result.days_to_zero and result.days_to_zero <= 3 else "📊 "
    msg = (
        f"{prefix}FlowGuard: Cash {_fmt_inr(result.available_cash_inr)}, "
        f"bills {_fmt_inr(result.total_obligations_inr)}. "
        f"Top action: {top.action.value} {_fmt_inr(top.amount_inr)} to {top.counterparty_name}."
    )
    return msg[:300]


# ─────────────────────────────────────────────
# NEGOTIATION EMAIL DRAFTS
# ─────────────────────────────────────────────

def draft_negotiation_email(
    decision: DecisionRecord,
    sender_name: str = "The Management",
    proposed_date: Optional[date] = None,
) -> str:
    """
    Tone is dictated by EmailTone (relationship_score-driven).
    The LLM narrates; the engine decided — this function bridges both.
    """
    amt  = _fmt_inr(decision.amount_inr)
    name = decision.counterparty_name
    prop = proposed_date.strftime("%d %B %Y") if proposed_date else "the earliest mutually convenient date"

    if decision.email_tone.value == "WARM_APOLOGETIC":
        return (
            f"Dear {name},\n\n"
            f"I hope this message finds you well. I wanted to reach out personally "
            f"regarding our payment of {amt}, which was due on "
            f"{decision.due_date.strftime('%d %B %Y')}.\n\n"
            f"Due to a temporary cash flow constraint, I would deeply appreciate your "
            f"understanding if we could reschedule this to {prop}. "
            f"We value our relationship immensely and will prioritise clearing this "
            f"as well as future payments on time.\n\n"
            f"Please let me know if this works for you. I remain grateful for your "
            f"continued partnership.\n\n"
            f"Warm regards,\n{sender_name}"
        )

    if decision.email_tone.value == "PROFESSIONAL_NEUTRAL":
        return (
            f"Dear {name},\n\n"
            f"I am writing to inform you that our payment of {amt} "
            f"(due {decision.due_date.strftime('%d %B %Y')}) will be rescheduled "
            f"to {prop} due to operational cash flow reasons.\n\n"
            f"We appreciate your understanding and will ensure timely payment "
            f"by the revised date.\n\n"
            f"Regards,\n{sender_name}"
        )

    # FIRM_BRIEF
    return (
        f"Dear {name},\n\n"
        f"Payment of {amt} rescheduled to {prop}.\n\n"
        f"Regards,\n{sender_name}"
    )
