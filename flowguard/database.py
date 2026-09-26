"""
flowguard/database.py
─────────────────────
SQLAlchemy ORM layer with SQLite backend.

Tables:
  obligations    – all extracted obligations (dedup by obligation_id)
  cash_snapshots – cash balance history
  engine_runs    – full engine run audit log
  decisions      – per-obligation decisions, linked to engine_runs
  file_imports   – tracks imported files to prevent re-import
  user_profile   – business and personal details
  transactions   – every cash movement (IN or OUT) across all mediums
                   De-duped by prefixed reference IDs:
                     U-<UPI TxnID>    : UPI
                     BC-<ChequeNo>    : Bank Cheque
                     R-<ReceiptNo>    : Receipt
                     LC-<seq>         : Liquid Cash
                     BT-<NEFT/RTGS>   : Bank Transfer / NEFT / RTGS
                     OL-<OrderRef>    : Online Payment (Razorpay, Paytm…)
                     DD-<No>          : Demand Draft
                     FG-<uuid8>       : Auto-generated (no external ref)

Designed for easy migration to PostgreSQL (just change the DB URL).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime
from enum import Enum as PyEnum
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# TRANSACTION MEDIUM ENUM + REF-ID PREFIX MAP
# ─────────────────────────────────────────────

class TxMedium(str, PyEnum):
    UPI           = "UPI"           # U-
    BANK_CHEQUE   = "BANK_CHEQUE"   # BC-
    RECEIPT       = "RECEIPT"       # R-
    LIQUID_CASH   = "LIQUID_CASH"   # LC-
    BANK_TRANSFER = "BANK_TRANSFER" # BT-  (NEFT / RTGS / IMPS)
    ONLINE        = "ONLINE"        # OL-  (Razorpay, Paytm, Stripe…)
    DEMAND_DRAFT  = "DEMAND_DRAFT"  # DD-
    AUTO          = "AUTO"          # FG-  (no external ref available)


# Prefix registry: medium → prefix string
TX_PREFIX: dict[str, str] = {
    TxMedium.UPI:           "U",
    TxMedium.BANK_CHEQUE:   "BC",
    TxMedium.RECEIPT:       "R",
    TxMedium.LIQUID_CASH:   "LC",
    TxMedium.BANK_TRANSFER: "BT",
    TxMedium.ONLINE:        "OL",
    TxMedium.DEMAND_DRAFT:  "DD",
    TxMedium.AUTO:          "FG",
}


def generate_txn_ref_id(medium: str, external_ref: Optional[str] = None) -> str:
    """
    Build a canonical, prefixed transaction reference ID.

    If the medium already carries an external reference (UPI txn ID, cheque
    number, receipt number, NEFT/RTGS UTR, …) we attach it directly:
        U-123456789012  BC-123456  R-INV2024001  BT-N24801234

    For instruments with no external ref (liquid cash, or truly unknown),
    we generate a unique FG-<8-char uuid> so the record is still deduplicated.
    """
    prefix = TX_PREFIX.get(medium, "FG")
    if external_ref and external_ref.strip():
        clean = external_ref.strip().upper().replace(" ", "")
        return f"{prefix}-{clean}"
    # Auto-generate when no external ref is available
    short_uid = uuid.uuid4().hex[:8].upper()
    return f"{prefix}-{short_uid}"


from sqlalchemy import (
    Column, String, Float, Integer, Boolean, Date, DateTime, Text,
    Enum as SAEnum, ForeignKey, create_engine, event,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import (
    DeclarativeBase, sessionmaker, relationship, Session,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# DATABASE PATH & ENGINE
# ─────────────────────────────────────────────

_DB_PATH = Path(__file__).resolve().parent.parent / "flowguard.db"
_DATABASE_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(
    _DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},  # SQLite + FastAPI
)

# Enable WAL mode for better concurrent reads
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ─────────────────────────────────────────────
# BASE
# ─────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# UNIVERSAL DEDUP KEY
# ─────────────────────────────────────────────

def compute_obligation_id(
    counterparty: str, amount: float, due_date: date
) -> str:
    """
    SHA-256 based dedup key.
    Two obligations with the same party + amount + due_date from ANY source
    (chat, CSV, invoice image, PDF) are treated as the SAME obligation.
    """
    normalized = (
        f"{counterparty.strip().lower()}"
        f"|{amount:.2f}"
        f"|{due_date.isoformat()}"
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def compute_file_hash(content: bytes) -> str:
    """SHA-256 of file content for import dedup."""
    return hashlib.sha256(content).hexdigest()


# ─────────────────────────────────────────────
# TABLE: obligations
# ─────────────────────────────────────────────

class ObligationRow(Base):
    __tablename__ = "obligations"

    obligation_id       = Column(String(16), primary_key=True)
    counterparty_name   = Column(String(200), nullable=False, index=True)
    description         = Column(Text, default="")
    amount_inr          = Column(Float, nullable=False)
    penalty_rate_annual  = Column(Float, default=0.0)
    due_date            = Column(Date, nullable=False, index=True)
    max_deferral_days   = Column(Integer, default=0)
    category            = Column(String(30), nullable=False, index=True)
    flexibility         = Column(String(20), nullable=False)
    relationship_score  = Column(Float, default=50.0)
    is_recurring        = Column(Boolean, default=False)
    parse_confidence    = Column(Float, default=1.0)
    notes               = Column(Text, nullable=True)

    # ── source tracking ──
    source_type         = Column(String(10), default="CHAT")  # CHAT|CSV|PDF|IMAGE|API
    source_file_hash    = Column(String(64), nullable=True)   # links to file_imports
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    decisions           = relationship("DecisionRow", back_populates="obligation",
                                       cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "obligation_id": self.obligation_id,
            "counterparty_name": self.counterparty_name,
            "description": self.description,
            "amount_inr": self.amount_inr,
            "penalty_rate_annual_pct": self.penalty_rate_annual,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "max_deferral_days": self.max_deferral_days,
            "category": self.category,
            "flexibility": self.flexibility,
            "relationship_score": self.relationship_score,
            "is_recurring": self.is_recurring,
            "source_type": self.source_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────
# TABLE: cash_snapshots
# ─────────────────────────────────────────────

class CashSnapshotRow(Base):
    __tablename__ = "cash_snapshots"

    snapshot_id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    available_cash_inr  = Column(Float, nullable=False)
    as_of_date          = Column(Date, nullable=False)
    is_verified         = Column(Boolean, default=False)
    source_type         = Column(String(10), default="CHAT")
    created_at          = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# TABLE: engine_runs
# ─────────────────────────────────────────────

class EngineRunRow(Base):
    __tablename__ = "engine_runs"

    run_id              = Column(String(36), primary_key=True)
    as_of_date          = Column(Date, nullable=False)
    available_cash_inr  = Column(Float, nullable=False)
    total_obligations   = Column(Float, nullable=False)
    cash_shortfall      = Column(Float, default=0.0)
    days_to_zero        = Column(Integer, nullable=True)
    num_obligations     = Column(Integer, nullable=False)
    raw_input           = Column(Text, nullable=True)
    source_type         = Column(String(10), default="CHAT")
    computed_at         = Column(DateTime, default=datetime.utcnow)

    # Relationships
    decisions           = relationship("DecisionRow", back_populates="engine_run")


# ─────────────────────────────────────────────
# TABLE: decisions
# ─────────────────────────────────────────────

class DecisionRow(Base):
    __tablename__ = "decisions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    run_id              = Column(String(36), ForeignKey("engine_runs.run_id"), nullable=False)
    obligation_id       = Column(String(16), ForeignKey("obligations.obligation_id"), nullable=False)
    counterparty_name   = Column(String(200), nullable=False)
    amount_inr          = Column(Float, nullable=False)
    consequence_score   = Column(Float, nullable=False)
    score_band          = Column(String(10), nullable=False)
    action              = Column(String(15), nullable=False)
    cash_allocated_inr  = Column(Float, default=0.0)
    penalty_per_day     = Column(Float, default=0.0)
    cot_reason          = Column(Text, default="")
    cot_tradeoff        = Column(Text, default="")
    cot_downstream      = Column(Text, default="")
    computed_at         = Column(DateTime, default=datetime.utcnow)

    # Relationships
    engine_run          = relationship("EngineRunRow", back_populates="decisions")
    obligation          = relationship("ObligationRow", back_populates="decisions")

    __table_args__ = (
        UniqueConstraint("run_id", "obligation_id", name="uq_run_obligation"),
        Index("ix_decisions_run", "run_id"),
    )


# ─────────────────────────────────────────────
# TABLE: file_imports
# ─────────────────────────────────────────────

class FileImportRow(Base):
    __tablename__ = "file_imports"

    file_hash           = Column(String(64), primary_key=True)
    filename            = Column(String(500), nullable=False)
    file_type           = Column(String(10), nullable=False)  # CSV|PDF|IMAGE
    file_size_bytes     = Column(Integer, nullable=False)
    obligations_found   = Column(Integer, default=0)
    obligations_new     = Column(Integer, default=0)  # how many were NOT duplicates
    obligations_updated = Column(Integer, default=0)  # how many were UPSERTed
    raw_text_extracted  = Column(Text, nullable=True)  # OCR/PDF text for audit
    imported_at         = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# TABLE: transactions
# ─────────────────────────────────────────────
#
# Each row = one atomic cash movement.
# Direction: "IN" (cash received) | "OUT" (cash paid out)
#
# ref_id is the PRIMARY KEY → automatic dedup:
#   U-321456789012   → re-importing the same UPI txn is silently ignored
#   BC-038271        → same cheque, same entry
#   FG-A3F591B0      → auto-generated when no external ref exists

class TransactionRow(Base):
    __tablename__ = "transactions"

    ref_id          = Column(String(80), primary_key=True)  # e.g.  U-9876543210
    medium          = Column(String(20), nullable=False)     # TxMedium value
    direction       = Column(String(3),  nullable=False)     # IN | OUT
    amount_inr      = Column(Float,      nullable=False)
    txn_date        = Column(Date,       nullable=False)

    counterparty    = Column(String(200), nullable=True)     # who paid / who received
    description     = Column(Text,        nullable=True)
    notes           = Column(Text,        nullable=True)

    # Raw reference from source (before prefixing) – for audit
    external_ref    = Column(String(200), nullable=True)     # UPI txn ID / cheque no …
    # Which file did this come from?
    source_file_hash = Column(String(64), nullable=True)
    source_type     = Column(String(10),  default="CHAT")    # CHAT|CSV|PDF|IMAGE|API

    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "ref_id":        self.ref_id,
            "medium":        self.medium,
            "direction":     self.direction,
            "amount_inr":    self.amount_inr,
            "txn_date":      self.txn_date.isoformat() if self.txn_date else None,
            "counterparty":  self.counterparty,
            "description":   self.description,
            "external_ref":  self.external_ref,
            "source_type":   self.source_type,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────
# TABLE: user_profile
# ─────────────────────────────────────────────

class UserProfileRow(Base):
    __tablename__ = "user_profile"

    id                  = Column(Integer, primary_key=True, default=1) # Single user for now
    full_name           = Column(String(200), nullable=True)
    business_name       = Column(String(200), nullable=True)
    industry            = Column(String(100), nullable=True)
    business_description = Column(Text, nullable=True)
    gstin               = Column(String(15), nullable=True)
    annual_turnover     = Column(String(50), nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "full_name": self.full_name,
            "business_name": self.business_name,
            "industry": self.industry,
            "business_description": self.business_description,
            "gstin": self.gstin,
            "annual_turnover": self.annual_turnover,
        }


# ─────────────────────────────────────────────
# CREATE ALL TABLES
# ─────────────────────────────────────────────

def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized at %s", _DB_PATH)


# ─────────────────────────────────────────────
# CRUD HELPERS
# ─────────────────────────────────────────────

def get_db() -> Session:
    """FastAPI dependency: yield a session, auto-close."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def upsert_obligation(db: Session, data: dict, source_type: str = "CHAT",
                      file_hash: Optional[str] = None) -> tuple[ObligationRow, bool]:
    """
    Insert or update an obligation. Returns (row, is_new).
    Uses obligation_id as the dedup key.
    """
    oid = data.get("obligation_id") or compute_obligation_id(
        data["counterparty_name"], data["amount_inr"],
        data["due_date"] if isinstance(data["due_date"], date)
        else date.fromisoformat(str(data["due_date"]))
    )

    existing = db.query(ObligationRow).filter_by(obligation_id=oid).first()
    if existing:
        # Update mutable fields — map input dict keys to ORM column names
        _field_map = {
            "description":            "description",
            "penalty_rate_annual_pct": "penalty_rate_annual",  # ← key fix
            "max_deferral_days":      "max_deferral_days",
            "category":               "category",
            "flexibility":            "flexibility",
            "relationship_score":     "relationship_score",
            "notes":                  "notes",
        }
        for src_key, db_col in _field_map.items():
            if src_key in data and data[src_key] is not None:
                setattr(existing, db_col, data[src_key])
        existing.updated_at = datetime.utcnow()
        db.commit()
        return existing, False

    due = data["due_date"]
    if isinstance(due, str):
        due = date.fromisoformat(due)

    row = ObligationRow(
        obligation_id=oid,
        counterparty_name=data["counterparty_name"],
        description=data.get("description", ""),
        amount_inr=data["amount_inr"],
        penalty_rate_annual=data.get("penalty_rate_annual_pct", 0.0),
        due_date=due,
        max_deferral_days=data.get("max_deferral_days", 0),
        category=data.get("category", "OTHER"),
        flexibility=data.get("flexibility", "DEFERRABLE"),
        relationship_score=data.get("relationship_score", 50.0),
        is_recurring=data.get("is_recurring", False),
        parse_confidence=data.get("parse_confidence", 1.0),
        notes=data.get("notes"),
        source_type=source_type,
        source_file_hash=file_hash,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def store_engine_run(db: Session, run_id: str, engine_result: dict,
                     raw_input: Optional[str] = None,
                     source_type: str = "CHAT") -> EngineRunRow:
    """Persist a full engine run + decisions."""
    run = EngineRunRow(
        run_id=run_id,
        as_of_date=date.today(),
        available_cash_inr=engine_result.get("available_cash_inr", 0),
        total_obligations=engine_result.get("total_obligations_inr", 0),
        cash_shortfall=engine_result.get("cash_shortfall_inr", 0),
        days_to_zero=engine_result.get("days_to_zero"),
        num_obligations=len(engine_result.get("decisions", [])),
        raw_input=raw_input,
        source_type=source_type,
    )
    db.add(run)

    for d in engine_result.get("decisions", []):
        dec = DecisionRow(
            run_id=run_id,
            obligation_id=d.get("obligation_id", ""),
            counterparty_name=d.get("counterparty_name", ""),
            amount_inr=d.get("amount_inr", 0),
            consequence_score=d.get("consequence_score", 0),
            score_band=d.get("score_band", "LOW"),
            action=d.get("action", "DEFER"),
            cash_allocated_inr=d.get("cash_allocated_inr", 0),
            penalty_per_day=d.get("penalty_per_day_inr", 0),
            cot_reason=d.get("cot_reason", ""),
            cot_tradeoff=d.get("cot_tradeoff", ""),
            cot_downstream=d.get("cot_downstream", ""),
        )
        db.add(dec)

    db.commit()
    return run


def check_file_imported(db: Session, file_hash: str) -> Optional[FileImportRow]:
    """Check if a file was already imported."""
    return db.query(FileImportRow).filter_by(file_hash=file_hash).first()


def record_file_import(db: Session, file_hash: str, filename: str,
                       file_type: str, file_size: int,
                       found: int, new: int, updated: int,
                       raw_text: Optional[str] = None) -> FileImportRow:
    """Record a file import for dedup tracking."""
    row = FileImportRow(
        file_hash=file_hash,
        filename=filename,
        file_type=file_type,
        file_size_bytes=file_size,
        obligations_found=found,
        obligations_new=new,
        obligations_updated=updated,
        raw_text_extracted=raw_text,
    )
    db.add(row)
    db.commit()
    return row


def record_cash_snapshot(db: Session, available_cash_inr: float, as_of_date: date,
                         source_type: str = "CHAT", is_verified: bool = False) -> CashSnapshotRow:
    """Persist a self-reported or verified cash balance snapshot."""
    row = CashSnapshotRow(
        available_cash_inr=available_cash_inr,
        as_of_date=as_of_date,
        is_verified=is_verified,
        source_type=source_type,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_latest_cash_snapshot(db: Session) -> Optional[CashSnapshotRow]:
    """Fetch the most recently recorded cash snapshot, if any."""
    return (
        db.query(CashSnapshotRow)
        .order_by(CashSnapshotRow.created_at.desc())
        .first()
    )


def get_all_obligations(db: Session, category: Optional[str] = None,
                        active_only: bool = True) -> list[ObligationRow]:
    """Fetch all obligations, optionally filtered."""
    q = db.query(ObligationRow)
    if category:
        q = q.filter(ObligationRow.category == category)
    if active_only:
        q = q.filter(ObligationRow.due_date >= date.today())
    return q.order_by(ObligationRow.due_date.asc()).all()


def get_run_history(db: Session, limit: int = 20) -> list[EngineRunRow]:
    """Fetch recent engine runs."""
    return (
        db.query(EngineRunRow)
        .order_by(EngineRunRow.computed_at.desc())
        .limit(limit)
        .all()
    )


def delete_obligation(db: Session, obligation_id: str) -> bool:
    """Delete an obligation by ID. Returns True if found."""
    row = db.query(ObligationRow).filter_by(obligation_id=obligation_id).first()
    if row:
        db.delete(row)
        db.commit()
        return True
    return False


# ─────────────────────────────────────────────
# TRANSACTION CRUD HELPERS
# ─────────────────────────────────────────────

def record_transaction(
    db: Session,
    medium: str,
    direction: str,              # "IN" | "OUT"
    amount_inr: float,
    txn_date: date,
    counterparty: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    external_ref: Optional[str] = None,
    source_type: str = "CHAT",
    source_file_hash: Optional[str] = None,
) -> tuple[TransactionRow, bool]:
    """
    Insert a transaction.
    Dedup is handled automatically: if ref_id already exists, the row
    is returned unchanged and is_new=False.

    Returns (row, is_new).
    """
    ref_id = generate_txn_ref_id(medium, external_ref)
    existing = db.query(TransactionRow).filter_by(ref_id=ref_id).first()
    if existing:
        return existing, False   # duplicate – already recorded

    row = TransactionRow(
        ref_id=ref_id,
        medium=medium,
        direction=direction.upper(),
        amount_inr=amount_inr,
        txn_date=txn_date,
        counterparty=counterparty,
        description=description,
        notes=notes,
        external_ref=external_ref,
        source_type=source_type,
        source_file_hash=source_file_hash,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def get_transactions(
    db: Session,
    direction: Optional[str] = None,     # "IN" | "OUT" | None (both)
    medium: Optional[str] = None,        # TxMedium value | None (all)
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 200,
) -> list[TransactionRow]:
    """Fetch transactions with optional filters."""
    q = db.query(TransactionRow)
    if direction:
        q = q.filter(TransactionRow.direction == direction.upper())
    if medium:
        q = q.filter(TransactionRow.medium == medium)
    if from_date:
        q = q.filter(TransactionRow.txn_date >= from_date)
    if to_date:
        q = q.filter(TransactionRow.txn_date <= to_date)
    return q.order_by(TransactionRow.txn_date.desc()).limit(limit).all()


def get_tx_summary(db: Session, from_date: Optional[date] = None,
                   to_date: Optional[date] = None) -> dict:
    """
    Returns total IN, total OUT and net balance for a date range.
    Useful for the engine to pull verified cash movements.
    """
    txns = get_transactions(db, from_date=from_date, to_date=to_date, limit=10000)
    total_in  = sum(t.amount_inr for t in txns if t.direction == "IN")
    total_out = sum(t.amount_inr for t in txns if t.direction == "OUT")
    by_medium: dict[str, dict] = {}
    for t in txns:
        m = t.medium
        if m not in by_medium:
            by_medium[m] = {"in": 0.0, "out": 0.0, "count": 0}
        if t.direction == "IN":
            by_medium[m]["in"] += t.amount_inr
        else:
            by_medium[m]["out"] += t.amount_inr
        by_medium[m]["count"] += 1
    return {
        "total_in_inr":  round(total_in, 2),
        "total_out_inr": round(total_out, 2),
        "net_inr":       round(total_in - total_out, 2),
        "count":         len(txns),
        "by_medium":     by_medium,
    }


def delete_transaction(db: Session, ref_id: str) -> bool:
    """Delete a transaction by ref_id. Returns True if found."""
    row = db.query(TransactionRow).filter_by(ref_id=ref_id).first()
    if row:
        db.delete(row)
        db.commit()
        return True
    return False


# ─────────────────────────────────────────────
# USER PROFILE HELPERS
# ─────────────────────────────────────────────

def get_user_profile(db: Session) -> Optional[UserProfileRow]:
    """Fetch the single user profile."""
    return db.query(UserProfileRow).filter_by(id=1).first()


def update_user_profile(db: Session, data: dict) -> UserProfileRow:
    """Create or update the user profile."""
    profile = db.query(UserProfileRow).filter_by(id=1).first()
    if not profile:
        profile = UserProfileRow(id=1)
        db.add(profile)

    for key in ("full_name", "business_name", "industry", 
                 "business_description", "gstin", "annual_turnover"):
        if key in data:
            setattr(profile, key, data[key])
    
    db.commit()
    db.refresh(profile)
    return profile


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def get_db_status() -> dict:
    """Return DB health info."""
    db = SessionLocal()
    try:
        obl_count = db.query(ObligationRow).count()
        run_count = db.query(EngineRunRow).count()
        file_count = db.query(FileImportRow).count()
        txn_count = db.query(TransactionRow).count()
        return {
            "status": "connected",
            "path": str(_DB_PATH),
            "obligations": obl_count,
            "engine_runs": run_count,
            "file_imports": file_count,
            "transactions": txn_count,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        db.close()

