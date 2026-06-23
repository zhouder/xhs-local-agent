# Safety Boundaries

Hard limits:

- No automatic comments.
- No automatic direct messages.
- No automatic likes.
- No interest browsing.
- No captcha bypass.
- No anti-detection or risk-control bypass.
- No cookie reading, export, persistence or reuse.
- XHS login is manual in the browser window opened by Playwright.

Publish safety:

- A note must be `pending_review` before approval.
- `/approve` and UI approval only move a note to `approved`; they never publish.
- Fill modes only fill the page and save a screenshot.
- The publish button is clicked only by `final_confirm`, only from `waiting_final_confirm`, and only when a fill screenshot exists.
- If the app cannot confidently verify success after clicking publish, the note is marked `publish_uncertain`.

Sensitive data:

- API keys are written only to local `.env`.
- `.env`, `.env.bak`, `data/`, database files, screenshots and logs must not be committed.
- Audit logs and pages must not display API keys, cookies, tokens or model thinking content.
