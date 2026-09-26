"""
flowguard/regulatory.py
───────────────────────
Authoritative, structured regulatory knowledge layer for Indian MSMEs.
Contains verified statutory rules, governing Acts, interest rates, and legal remedies.

RULE: The LLM and Scoring Engine must consume these structured rules rather than
hallucinating legal consequences or universal penalty numbers.
"""

from __future__ import annotations
from typing import Optional


REGULATORY_RULES = {
    "STATUTORY_GST": {
        "category": "STATUTORY",
        "sub_type": "GST",
        "jurisdiction": "India (Union & States)",
        "governing_act": "Central Goods and Services Tax Act, 2017",
        "interest_provision": "Section 50(1): Simple interest at 18% p.a. on net tax liability paid after due date.",
        "penalty_or_fee": "Section 47: Late fee on return filing (₹50/day for regular, ₹20/day for nil return, subject to statutory caps).",
        "legal_consequences": "E-way bill blocking after 2 consecutive periods of non-filing (Rule 138E); registration suspension (Rule 21A); prosecution under Section 132 for tax evasion exceeding ₹2 Crore.",
        "standard_due_day": "20th of subsequent month for GSTR-3B (monthly filers).",
        "is_verified": True,
        "confidence": 0.98,
        "conditions": "Applies to GST-registered entities with outward supplies.",
    },
    "STATUTORY_TDS": {
        "category": "STATUTORY",
        "sub_type": "TDS",
        "jurisdiction": "India (Union)",
        "governing_act": "Income-tax Act, 1961",
        "interest_provision": "Section 201(1A)(ii): 1.5% simple interest per month (or part of month) from date of deduction to date of actual payment.",
        "penalty_or_fee": "Section 271C (penalty equal to tax amount); Section 234E late filing fee of ₹200/day for delayed quarterly returns.",
        "legal_consequences": "Disallowance of 30% of expenditure under Section 40(a)(ia); prosecution under Section 276B (3 months to 7 years rigorous imprisonment).",
        "standard_due_day": "7th of the following month (30th April for March deduction).",
        "is_verified": True,
        "confidence": 0.98,
        "conditions": "Applies when tax is deducted at source on specified payments.",
    },
    "STATUTORY_PF": {
        "category": "STATUTORY",
        "sub_type": "EPF",
        "jurisdiction": "India (Union)",
        "governing_act": "Employees' Provident Funds and Miscellaneous Provisions Act, 1952",
        "interest_provision": "Section 7Q: Simple interest at 12% p.a. on delayed remittances.",
        "penalty_or_fee": "Section 14B: Graded damages up to 25% p.a. depending on duration of delay.",
        "legal_consequences": "Withholding employee's contribution constitutes criminal breach of trust under Bharatiya Nyaya Sanhita (BNS) / IPC Section 405/406; recovery proceedings including attachment of bank accounts (Section 8B).",
        "standard_due_day": "15th of the following month.",
        "is_verified": True,
        "confidence": 0.98,
        "conditions": "Mandatory for establishments with 20+ employees.",
    },
    "STATUTORY_ESI": {
        "category": "STATUTORY",
        "sub_type": "ESIC",
        "jurisdiction": "India (Union)",
        "governing_act": "Employees' State Insurance Act, 1948",
        "interest_provision": "Regulation 31A: Simple interest at 12% p.a. on overdue contributions.",
        "penalty_or_fee": "Regulation 31C: Damages up to 25% p.a. of overdue contribution.",
        "legal_consequences": "Prosecution under Section 85; attachment of bank accounts by ESI Recovery Officer under Section 45C.",
        "standard_due_day": "15th of the following month.",
        "is_verified": True,
        "confidence": 0.98,
        "conditions": "Applicable to factories and covered establishments employing 10+ employees with wages up to ₹21,000/month.",
    },
    "SECURED_LOAN": {
        "category": "SECURED_LOAN",
        "sub_type": "BANK_OR_NBFC_LOAN",
        "jurisdiction": "India",
        "governing_act": "RBI Master Directions on Prudential Norms on Income Recognition and Asset Classification (IRAC); SARFAESI Act, 2002",
        "interest_provision": "Contractual rate + RBI Fair Practices Code for penal charges (no compounding of penal charges as per RBI 2024 guidelines).",
        "penalty_or_fee": "Penal charges as stipulated in the loan agreement, not exceeding reasonable levels.",
        "legal_consequences": "NPA (Non-Performing Asset) classification after 90 days of overdue principal/interest; CIBIL/commercial credit score degradation; SARFAESI Section 13(2) 60-day demand notice followed by possession of secured assets.",
        "standard_due_day": "Agreed EMI / repayment date as per sanction letter.",
        "is_verified": True,
        "confidence": 0.95,
        "conditions": "Depends on loan agreement terms and institutional lender classification.",
    },
    "SALARY": {
        "category": "SALARY",
        "sub_type": "WAGES",
        "jurisdiction": "India",
        "governing_act": "Payment of Wages Act, 1936 / Code on Wages, 2019 / State Shops and Establishments Acts",
        "interest_provision": "No statutory daily interest; Labour Authority can award compensation up to 10 times the delayed wage under Section 15(3).",
        "penalty_or_fee": "Statutory fine for delayed payment upon conviction.",
        "legal_consequences": "Complaint before Labour Commissioner / Industrial Tribunal; operational strike/attrition risk; reputation damage.",
        "standard_due_day": "7th to 10th of the following month (depending on employee count and state rules).",
        "is_verified": True,
        "confidence": 0.95,
        "conditions": "Applicable to employed staff.",
    },
    "RENT": {
        "category": "RENT",
        "sub_type": "COMMERCIAL_LEASE",
        "jurisdiction": "India (State-specific)",
        "governing_act": "Transfer of Property Act, 1882 (Section 106) and State Rent Control / Tenancy Acts",
        "interest_provision": "Contractual penal interest as agreed in lease deed.",
        "penalty_or_fee": "Governed exclusively by lease terms; possible loss of security deposit.",
        "legal_consequences": "Section 106 eviction notice (minimum 15 days notice for month-to-month leases); civil suit for recovery and mesne profits.",
        "standard_due_day": "Contractual (typically 1st to 5th of the month).",
        "is_verified": True,
        "confidence": 0.90,
        "conditions": "Subject to terms of registered/unregistered lease agreement.",
    },
    "TRADE_PAYABLE": {
        "category": "TRADE_PAYABLE",
        "sub_type": "SUPPLIER_INVOICE",
        "jurisdiction": "India",
        "governing_act": "Micro, Small and Medium Enterprises Development (MSMED) Act, 2006",
        "interest_provision": "Section 16: Compound interest with monthly rests at 3x the RBI Bank Rate for payments delayed beyond statutory limit (max 45 days) to registered MSME suppliers.",
        "penalty_or_fee": "Income-tax disallowance of delayed interest paid to MSMEs (Section 23 MSMED Act).",
        "legal_consequences": "Supplier dispute before MSME Facilitation Council (SAMADHAAN); commercial relationship severance; stop-supply notice.",
        "standard_due_day": "Credit terms on invoice (not to exceed 45 days if supplier is Udyam-registered).",
        "is_verified": True,
        "confidence": 0.92,
        "conditions": "Compound interest applies if supplier has valid Udyam registration and buyer exceeded agreed/45-day window.",
    },
    "UTILITY": {
        "category": "UTILITY",
        "sub_type": "ELECTRICITY_WATER_INTERNET",
        "jurisdiction": "India (State DISCOMs / Telecom Regulations)",
        "governing_act": "Electricity Act, 2003 (Section 56)",
        "interest_provision": "Surcharge / Delayed Payment Surcharge (DPS) as per State Electricity Regulatory Commission (SERC) tariff orders.",
        "penalty_or_fee": "Reconnection fee following disconnection.",
        "legal_consequences": "Section 56 Electricity Act allows disconnection after 15 clear days notice in writing; immediate operational downtime.",
        "standard_due_day": "Billing cycle due date printed on bill.",
        "is_verified": True,
        "confidence": 0.92,
        "conditions": "Disconnection requires mandatory prior statutory notice.",
    },
    "OTHER": {
        "category": "OTHER",
        "sub_type": "MISCELLANEOUS",
        "jurisdiction": "India",
        "governing_act": "Indian Contract Act, 1872",
        "interest_provision": "Contractual only.",
        "penalty_or_fee": "None statutory.",
        "legal_consequences": "Civil contract breach only.",
        "standard_due_day": "As agreed.",
        "is_verified": True,
        "confidence": 0.85,
        "conditions": "General commercial agreements.",
    },
}


def get_regulatory_rule(category: str, sub_type: Optional[str] = None) -> dict:
    """Fetch verified regulatory rule for a category, or a safe unverified fallback."""
    cat = (category or "OTHER").upper().strip()
    
    # Sub-type matching for statutory
    if cat == "STATUTORY" and sub_type:
        st = sub_type.upper().strip()
        key = f"STATUTORY_{st}"
        if key in REGULATORY_RULES:
            return REGULATORY_RULES[key]

    if cat in REGULATORY_RULES:
        return REGULATORY_RULES[cat]

    for key, rule in REGULATORY_RULES.items():
        if rule.get("category") == cat:
            return rule

    return {
        "category": cat,
        "sub_type": "UNKNOWN",
        "is_verified": False,
        "confidence": 0.0,
        "legal_consequences": "Regulatory consequence could not be verified.",
        "interest_provision": "Statutory interest rate could not be verified.",
        "governing_act": "Unknown / not applicable",
    }


def format_regulatory_context(category: str, sub_type: Optional[str] = None) -> str:
    """Format structured regulatory rule for injection into LLM prompts or COT."""
    rule = get_regulatory_rule(category, sub_type)
    if not rule.get("is_verified", False):
        return "Regulatory consequence could not be verified."

    parts = [
        f"Statutory authority: {rule.get('governing_act', 'Indian Law')}",
        f"Interest rule: {rule.get('interest_provision', 'Standard contractual rate')}",
        f"Consequences: {rule.get('legal_consequences', 'General contract remedies')}",
    ]
    return " | ".join(parts)
