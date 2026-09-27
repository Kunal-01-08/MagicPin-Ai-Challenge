# Vera Merchant Assistant

## Approach

`bot.py` implements a deterministic `compose(category, merchant, trigger, customer=None)` function. It routes by trigger kind and builds messages from the supplied category, merchant, trigger, and customer contexts. It uses category catalog offers, source-backed digest items, merchant performance facts, and the customer's recorded relationship details when those are available. It does not call an external LLM or require an API key.

The module also exposes the five HTTP endpoints described in `challenge-testing-brief.md`: `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata`. Context is held in memory for the process lifetime; context updates follow version ordering, and sent suppression keys are deduplicated. Reply handling recognizes opt-outs, common WhatsApp canned replies, and clear acceptance.

For optional simulator scoring, `judge_simulator.py` loads local settings from `.env`; add a rotated provider key to `LLM_API_KEY` before running it. `.env` is ignored by Git.

## Run

Install `requirements.txt`, then start locally with `uvicorn bot:app --host 0.0.0.0 --port 8080`; `Procfile` supplies the production command for hosts that support it. The deployment platform must expose the service over HTTPS; use `/v1/healthz` as its health check. Set `TEAM_NAME`, comma-separated `TEAM_MEMBERS`, `CONTACT_EMAIL`, and optionally `APP_VERSION` in the deployment environment so `/v1/metadata` identifies the submitting team.

## Tradeoffs

- Rules make outputs repeatable and easy to inspect, but messages are less flexible than LLM-generated copy.
- When a customer profile lacks consent for the trigger's purpose, the composer returns an empty body with `cta: none`; the canonical JSONL keeps that test pair and records why no message is sent.
- Generated placeholder triggers have limited facts, so their messages stay general instead of filling in missing details.
- The HTTP service uses in-memory state and is intended for the challenge harness, not production deployment.

## Useful additional context

More reliable, trigger-specific customer consent scopes; real available appointment slots and current merchant offers; and complete payloads for generated triggers would improve personalization while keeping messages grounded.

## Artifacts

- `bot.py` — composer and HTTP API
- `submission.jsonl` — one result for each of the 30 canonical test pairs
- `dataset/expanded/` — deterministic expanded challenge dataset and canonical test pairs
