# Safety Boundaries

Hard limits:

- No automatic comments.
- No automatic direct messages.
- No automatic likes.
- No interest browsing.
- No captcha bypass.
- No anti-detection or risk-control bypass.
- No cookie reading, export, printing or committing.
- XHS login is manual.

Browser login:

- Real fill modes use a dedicated browser profile under `data/browser-profiles/`.
- The app may let the browser keep its own login state, but code must not call cookie read/export APIs.
- `dry_run` is local simulation and never opens a browser.

Publish safety:

- Approval only changes `pending_review` to `approved`.
- `dry_run` cannot publish.
- `fill_only` cannot publish.
- The publish button is clicked only by final confirmation, only from `waiting_final_confirm`, and only after a fill screenshot exists.
- Unverified publish result becomes `publish_uncertain`.

Sensitive data:

- API keys are written only to local `.env`.
- `.env`, `.env.bak`, `data/`, browser profiles, database files, screenshots and logs must not be committed.
- Audit logs and pages must not display API keys, cookies, tokens or model thinking content.
