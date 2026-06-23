# Feishu Commands

MVP endpoints:

- `POST /commands/mock` for local form/curl testing.
- `POST /webhooks/feishu` for a basic Feishu-style JSON payload. Accepted fields: `command` or `text`.

Configuration remains in `.env`:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`

Supported commands:

- `/status`: show counts and pause state.
- `/drafts`: list draft notes.
- `/pending`: list notes waiting review.
- `/approve <note_id>`: only changes `pending_review` to `approved`.
- `/reject <note_id>`: rejects a pending note.
- `/waiting`: list notes waiting final confirmation.
- `/final_confirm <note_id>`: only allowed when the note is `waiting_final_confirm` and has a fill screenshot.
- `/cancel <note_id>`: cancel a note waiting final confirmation.
- `/pause`: pause the agent policy.
- `/resume`: resume the agent policy.

All commands write to `command_events` and `audit_logs`. Responses must not include API keys, cookies, tokens or model thinking content. Screenshot delivery is an MVP path string response; direct Feishu image upload can be added later.
