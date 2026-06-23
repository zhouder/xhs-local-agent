# Publish Flow

Statuses:

- `draft`
- `pending_review`
- `approved`
- `publishing`
- `waiting_final_confirm`
- `published`
- `publish_uncertain`
- `failed`
- `rejected`
- `returned_to_edit`
- `cancelled`

Allowed main transitions:

1. `draft -> pending_review`
2. `pending_review -> approved / rejected / draft`
3. `approved -> publishing`
4. `publishing -> waiting_final_confirm / failed`
5. `waiting_final_confirm -> published / publish_uncertain / cancelled / returned_to_edit / failed`
6. `returned_to_edit -> draft`

Modes:

- `dry_run`: fills and screenshots, never clicks publish.
- `fill_only`: fills the real publish page and screenshots, never clicks publish.
- `publish_after_final_confirm`: fills and screenshots first; clicking publish requires the final confirm action.

Final confirmation:

1. Open `/notes/<note_id>/final-review`.
2. Review title, body, hashtags, media paths and screenshot.
3. Click “最终确认并发布”.
4. The app reopens the publish page, refills the content, clicks the publish button and saves another screenshot.
5. The note becomes `publish_uncertain` unless a later explicit manual verification marks it published.

Failure handling:

- Browser failures save a screenshot or placeholder PNG.
- Failures are written to `browser_errors` and `audit_logs`.
- Selector failures include the selector name and step.
