# Feedback page toggle

## Problem

The Feedback page (`/help/feedback`) is always visible in the Help menu and
always reachable. There's no way to hide it. The user wants it hidden by
default, with a setting to turn it on.

## Design

### Backend

New singleton document `AppSettings` (`backend/app/models/app_settings.py`),
following the existing `ResourceLimits` / `AiRouting` singleton pattern (see
`backend/app/models/resource_limits.py`): a fixed `SINGLETON_ID`, a
`load()` classmethod that upserts on first read, no `owner` field (matches
the reasoning in `resource_limits.py` — one machine, not profile-scoped).

```python
class AppSettings(Document):
    SINGLETON_ID: ClassVar[str] = "app_settings"
    id: str = Field(default=SINGLETON_ID)
    feedback_enabled: bool = False
    updated_at: datetime = Field(default_factory=utcnow)
```

New endpoints in `backend/app/api/v1/settings.py`, following the
`GET /resources` / `PUT /resources` pair already in that file:

- `GET /settings/general` → `{ feedback_enabled: bool }`
- `PUT /settings/general` → accepts `{ feedback_enabled: bool }`, saves, and
  returns the updated value.

### Frontend

**New settings tab.** A new `SettingsGeneral.tsx` component, structured like
`SettingsResources.tsx`'s toggle: `useQuery` loads the current value into
local state via `useEffect`, a checkbox edits local state, a `useMutation`
persists it via `api.setGeneralSettings(...)` and invalidates the query on
success.

New route `/settings/general` in `App.tsx`, and a new "General" entry in
`SettingsNav.tsx`'s nav array.

**API client** (`frontend/src/api/client.ts`): add `generalSettings()` and
`setGeneralSettings(body)`, following the `resourceLimits`/`setResourceLimits`
pair.

**Menu visibility.** `Header.tsx` currently renders `HELP_ITEMS` (a static
array) unconditionally into the Help dropdown. Add a `useQuery` for
`["settings", "general"]` in `Header.tsx`, and filter `HELP_ITEMS` to exclude
the Feedback entry when `feedback_enabled` is not `true`. Default to hidden
while loading (matches "off by default").

**Route guard.** In `App.tsx`, the `/help/feedback` route currently renders
`<HelpFeedback />` directly. Wrap it in a small guard component
(`FeedbackRoute`) that reads the same `["settings", "general"]` query and
either renders `<HelpFeedback />` (when enabled) or `<Navigate to="/help" />`
(when disabled or still loading). This blocks direct navigation to the URL
when the flag is off, per the user's explicit choice.

### Data flow

```
AppSettings (Mongo, singleton)
   ↕ GET/PUT /settings/general
Header.tsx / FeedbackRoute (useQuery, same query key)
   → Header: filters Feedback out of Help dropdown when off
   → FeedbackRoute: redirects /help/feedback → /help when off
SettingsGeneral.tsx (checkbox) → PUT /settings/general → invalidates query
```

Both consumers (`Header.tsx`, `FeedbackRoute`) share the same React Query
key, so toggling the setting in `SettingsGeneral.tsx` and invalidating that
key updates both the menu and the route guard without a page reload.

### Default

`feedback_enabled` defaults to `false`. Fresh installs and existing installs
alike get Feedback hidden until a user opts in via Settings → General.

## Out of scope

- No feature-flag framework — this is a single hand-rolled boolean, matching
  the codebase's existing pattern of small per-purpose singleton settings
  documents rather than a generic flag system (there is no existing
  feature-flag precedent in the app, confirmed by exploration).
- No migration needed: `AppSettings.load()` upserts a default document on
  first read, same as `ResourceLimits` and `AiRouting`.
