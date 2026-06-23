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

Modes:

- `dry_run`: pure local simulation. It does not open XHS, Chrome, Edge or Chromium. It validates fields/assets and creates a local preview.
- `fill_only`: opens the real XHS publish page, waits for manual login when needed, fills the page, screenshots, and never clicks publish.
- `publish_after_final_confirm`: same fill step first. The publish button is clicked only from the final confirmation page.

Final confirmation:

1. Open `/notes/<note_id>/final-review`.
2. Review title, body, hashtags, media, mode and screenshot/preview.
3. `dry_run` previews cannot publish.
4. For real fill modes, click `最终确认并发布`.
5. The app reopens the publish page, refills content, clicks publish and saves a screenshot.
6. If success cannot be verified, status becomes `publish_uncertain`.

Failure handling:

- Browser closed by user returns a friendly Chinese error.
- Selector failures include the selector name and candidate list in `browser_errors`.
- Browser failures save a screenshot or placeholder PNG and write `audit_logs`.
