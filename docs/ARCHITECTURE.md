# Architecture

The app is a local FastAPI + SQLite + Playwright workflow for XHS content production.

Main modules:

- `app/main.py`: HTML routes, command endpoints, scheduler actions.
- `app/models.py`: SQLite models for notes, providers, media assets, plans, audit logs, browser errors, command events, and scheduled slots.
- `app/ai/*`: mock, OpenAI-compatible, Anthropic Messages, DeepSeek, GLM and related adapters.
- `app/services/state_machine.py`: note status transition guard.
- `app/services/notes.py`: draft generation, regeneration and edit invalidation.
- `app/services/review.py`: submit, approve and reject.
- `app/services/materials.py`: local image path parsing and validation.
- `app/services/publish.py`: safe publish orchestration.
- `app/browser/xhs.py`: Playwright fill and final-confirm click logic. Selectors are loaded only from `app/browser/selectors/xhs.yaml`.
- `app/services/content_plans.py`: content plan creation and batch draft generation.
- `app/services/commands.py`: mock/Feishu command parser and executor.
- `app/services/scheduler.py`: conservative scheduler that fills approved notes only and stops at final confirmation.

Data stays local. API keys stay in `.env`; SQLite stores only environment variable names and configuration status. Screenshots are stored under `data/screenshots` and are not tracked by git.
