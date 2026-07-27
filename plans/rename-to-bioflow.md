# Rename Project to BioFlow

**Goal:** Change the user-facing name "BioinfoHelper" to "BioFlow" in the UI and API metadata. Internal package names, directory names, database names, storage paths, and volume mounts remain unchanged to avoid disrupting existing data.

---

## Scope

Only the 3 user-visible "BioinfoHelper" references are changed. No other renames.

---

## Files to Change

### 1. API application title (Swagger/OpenAPI docs)

**File:** `backend/app/main.py` line 52
- `title="BioinfoHelper"` → `title="BioFlow"`

### 2. HTML page title (browser tab)

**File:** `frontend/index.html` line 6
- `<title>BioinfoHelper</title>` → `<title>BioFlow</title>`

### 3. Header brand text (top-left of UI)

**File:** `frontend/src/components/Header.tsx` line 31
- `<span>BioinfoHelper</span>` → `<span>BioFlow</span>`

---

## Execution Order

1. ✅ Create this plan
2. Patch `backend/app/main.py`
3. Patch `frontend/index.html`
4. Patch `frontend/src/components/Header.tsx`
5. Verify: `make up` — header shows "BioFlow"

---

## Not changing (to preserve data)

- Volume path `/Volumes/ModelExtension/BioinfoHelper` — changing this would orphan stored data
- Database name `biopipe` — would require a new MongoDB
- Package names `biopipe-backend` / `biopipe-frontend` — internal only
- Directory name `local-bio-pipeliner` — internal only
- Storage sentinel `.biopipe/` — internal only
- SRA tool identifier — internal only
- README title — will update if you want but it's a doc, not code
