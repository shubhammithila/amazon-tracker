## Fix pass: plan_id validation

**Status:** Complete  
**Commit:** `436ad02`  
**Test summary:** 2 parametrized cases added ("abc", []); 1069 total tests pass

**Mutation outcome:**
- `"abc"` (string): Raised unhandled `ValueError: invalid literal for int() with base 10: 'abc'` at line 2586
- `[]` (empty list): Silently fell back to active plan (200 OK), bypassing the intended explicit plan_id

Both failures demonstrate the bug: malformed plan_id produced either a 500 (unhandled exception) or silent fallback instead of a 400 naming the field.

**tests/test_invoice_save.py:** All 26 tests pass (GST series protection intact)

**POST /items confirmation:** Line 1202 still has unguarded `int(plan_id)` cast — deliberately untouched per scope limit
