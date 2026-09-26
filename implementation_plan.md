# FlowGuard — Full Integration Audit & Fix Plan

## Summary

After inspecting every source file, I've identified **28 bugs** across integration, hallucination risk, Pydantic v2 compatibility, database, and security categories. Fixes are grouped by severity and component. Architecture is otherwise sound — the engine/LLM separation is good; specific integration points are broken.

---

## Architecture Map (Actual)

```
chat.html
  ↓ POST /pipeline (NLPParseRequest)
FastAPI main.py
  ↓ _try_groq_parse() → groq_client.groq_parse_input()
      ↓ LLM extracts intent + obligations + filter_query + bot_reply
  ↓ Pydantic Obligation validation  [BUG: invents missing dates/amounts]
  ↓ upsert_obligation() (INGEST)    [BUG: update branch key mismatch]
  ↓ run_engine() (STATUS)            [DETERMINISTIC ✓]
  ↓ groq_rewrite_cot() per decision  [LLM overwrites deterministic COT]
  ↓ narrate_result()
  ↓ JSON response → chat.html

File upload:
  ↓ POST /upload/csv|pdf|image
  ↓ file_ingest.py
      ↓ [BUG: Pydantic v1 @validator API used → breaks on Pydantic ≥2]
      ↓ [BUG: _groq_structure_file bypasses safe wrapper, temp=0.1]
      ↓ [BUG: unknown category "UNSECURED_LOAN" not in enum]
      ↓ ValidatedObligation [BUG: date fallback invents date]
      ↓ upsert_obligation() → database
```

---

## Bugs Found

| # | Bug | File | Severity | Fix |
|---|-----|------|----------|-----|
| 1 | `compute_file_hash` defined TWICE | database.py:98,153 | Medium | Remove first definition |
| 2 | `upsert_obligation` update branch: checks `penalty_rate_annual_pct` key but field is stored as `penalty_rate_annual` — never updates penalty | database.py:408-412 | High | Fix key mapping |
| 3 | `_try_groq_parse` invents due_date when LLM omits it | main.py:312 | **Critical** | Set to `None`, skip obligation |
| 4 | `_try_groq_parse` invents amount=0 when missing (fails validation silently) | main.py:325 | High | Validate before building Obligation |
| 5 | `ValidatedObligation` uses Pydantic v1 `@validator` → breaks on Pydantic v2 | file_ingest.py:116 | **Critical** | Migrate to `@field_validator` |
| 6 | `ValidatedObligation.dict()` → Pydantic v2 needs `.model_dump()` | file_ingest.py:250 | **Critical** | Use `.model_dump()` |
| 7 | `ValidatedObligation.parse_due_date` returns `date.today()` on parse failure — invents date | file_ingest.py:137 | **Critical** | Raise ValueError instead |
| 8 | `_groq_structure_file` uses `temperature=0.1` (non-deterministic extraction) | file_ingest.py:353 | High | Set temperature=0 |
| 9 | `_groq_structure_file` directly uses private `_client`, bypasses retries/error-handling | file_ingest.py:344-367 | High | Refactor to use `_groq_chat()` helper |
| 10 | `_GROQ_FILE_PROMPT` lists "UNSECURED_LOAN" and "INSURANCE" categories not in enum | file_ingest.py:306 | High | Align to actual `ObligationCategory` enum |
| 11 | `_COT_SYSTEM` hardcodes regulatory figures LLM presents as facts | groq_client.py:437-441 | **Critical** | Add explicit disclaimer; mark as approximate |
| 12 | `delete_obligation` FK cascade not set — deleting obligation with decisions causes FK error | database.py:528 | High | Add `cascade="all, delete-orphan"` or handle in code |
| 13 | `get_db_status` opens session without `try/finally` — session leaks on exception | database.py:678 | Medium | Wrap in try/finally |
| 14 | `store_engine_run` never called from `/pipeline` endpoint — no DB persistence of runs | main.py:556+ | Medium | Call `store_engine_run` after scoring |
| 15 | `get_import_capabilities` reports wrong model name | file_ingest.py:768 | Low | Fix string |
| 16 | `_groq_structure_file` does not use `response_format=json_object` — may return markdown | file_ingest.py:346 | High | Add response_format or strip markdown more robustly |
| 17 | No file size limit on uploads — any size accepted | main.py:843+ | High | Add 10MB cap |
| 18 | `run_id` only 8 chars — collision-prone | scorer.py:718 | Low | Use full UUID |
| 19 | `import_pdf` sets `success=True` even when no obligations found | file_ingest.py:653 | Medium | Only set success when something was extracted |
| 20 | `_parse_system` prompt instructs LLM to invent default dates | groq_client.py:211-214 | **Critical** | Remove default date invention; keep null for unknown dates |
| 21 | `chat.html` score_band injected directly into CSS class without allowlist | chat.html:285 | Low | Add allowlist validation |
| 22 | Provenance/source tracking missing from all LLM outputs | models.py, groq_client.py | Medium | Add `_source` metadata to responses |
| 23 | `_PARSE_SYSTEM` instructs LLM to compute stats (shortfall arithmetic) | groq_client.py:237-246 | High | Remove arithmetic from LLM; let Python compute |
| 24 | `date.today()` used as `reference_date` but timezone is UTC not IST | main.py:359, 563 | Medium | Use IST-aware date |
| 25 | `obligation_id` computed differently in main.py vs database.py vs parser.py | main.py:320, database.py:145, parser.py:331 | High | Single canonical function |
| 26 | `GSTIN` validation missing — any string accepted | database.py:354, main.py:947 | Low | Basic regex for 15-char GSTIN |
| 27 | WhatsApp/pipeline response leaks full stack trace on internal errors | main.py:1103-1113 | Medium | Filter trace from user-facing detail |
| 28 | `_groq_chat` catches ALL exceptions — including Pydantic errors silently | groq_client.py:102 | Medium | Log with more detail |

---

## Hallucination Risks

| Risk | Location | Why | Mitigation |
|------|----------|-----|-----------|
| LLM invents due dates | `_try_groq_parse` line 312 | Default fallback `+7 days` | Reject obligations with no explicit date |
| LLM recites regulatory figures as fact | `_COT_SYSTEM` | Hard-coded "facts" in system prompt | Mark as approximate; add disclaimer |
| LLM computes shortfall | `_PARSE_SYSTEM` STEP 4 | Asks LLM to do arithmetic | Python computes all stats; remove from LLM |
| LLM default dates in system prompt | `_PARSE_SYSTEM` STEP 3 | "DEFAULT DUE DATES: STATUTORY → next 20th" | LLM should output `null` for unknown dates |
| Missing field becomes invented | `_groq_structure_file` | No strict null handling | Require explicit nulls for missing fields |

---

## Proposed Changes

### Component 1: database.py

#### [MODIFY] database.py
- Remove duplicate `compute_file_hash` (line 98-100)
- Fix `upsert_obligation` update branch key mapping for `penalty_rate_annual_pct`
- Add `cascade="all, delete-orphan"` on `decisions` relationship in `ObligationRow`
- Wrap `get_db_status` in `try/finally`

---

### Component 2: groq_client.py

#### [MODIFY] groq_client.py
- In `_PARSE_SYSTEM`: Remove DEFAULT DUE DATES section (stop inventing dates)
- In `_PARSE_SYSTEM`: Remove STEP 4 (COMPUTE INLINE STATS) — Python does arithmetic
- In `_COT_SYSTEM`: Add explicit disclaimer that regulatory figures are approximate
- In `_COT_SYSTEM`: Remove hardcoded "Rs.100/day", "NPA after 90 days" as facts

---

### Component 3: file_ingest.py

#### [MODIFY] file_ingest.py
- Migrate `@validator` → `@field_validator` (Pydantic v2)
- Change `.dict()` → `.model_dump()`
- Change `parse_due_date` to raise `ValueError` instead of returning `date.today()`
- Fix `_groq_structure_file` to use `_groq_chat()` helper (retries, json mode)
- Fix temperature=0 in file structuring
- Fix category list in `_GROQ_FILE_PROMPT` to match enum
- Add file size limit (10MB)
- Only set `success=True` when obligations or transactions were found

---

### Component 4: main.py

#### [MODIFY] main.py
- In `_try_groq_parse`: Skip obligations where `due_date` is None or `amount_inr <= 0`
- Add file size validation to all upload endpoints
- Use IST-aware date for reference date
- Call `store_engine_run` from `/pipeline` STATUS branch
- Use full UUID for `run_id` (fix in scorer.py)

---

### Component 5: scorer.py

#### [MODIFY] scorer.py  
- Use full UUID (not truncated 8 chars) for `run_id`

---

### Component 6: chat.html

#### [MODIFY] chat.html
- Allowlist `score_band` values before injecting into class name

---

### Component 7: [NEW] tests/test_flowguard.py

Full test suite covering:
- Money parsing
- Date parsing  
- Intent classification
- Scoring (deterministic)
- Hallucination tests (missing fields → null, not invented)
- File deduplication
- API integration tests

---

## Verification Plan

### Automated Tests
```bash
pytest tests/test_flowguard.py -v
```

### End-to-End Journey Tests
1. `POST /pipeline` with "GST ₹42,000 due September 20, EMI ₹28,500 due September 25, cash ₹1.5 lakh"
2. `POST /upload/csv` with test CSV
3. `GET /obligations`
4. "What should I pay first?" → verify deterministic scorer drives ranking
5. "GST is due soon" → verify no invented amount/date
