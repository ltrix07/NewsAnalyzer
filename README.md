# newsAnalyzer

Personal news aggregator with a verification cascade.

`make up && make install && make run && make lint`

See [AGENTS.md](AGENTS.md).

## User profile seed

After applying migrations, import or refresh the configured single-user profile with
`uv run python -m engine users seed-self`. The command reads `PROFILE_NAME` (default
`volodymyr`), its YAML profile, `TELEGRAM_CHAT_ID`, and `UI_LANGUAGE`; it is safe to run again.

## Tracked-link redirect service

Run `uv run python -m web` to listen on `127.0.0.1:8080`; `--host` and `--port` override the
bind address. The service expects the shared `DATABASE_URL` and must sit behind a TLS reverse
proxy. Set `LINK_TRACKING_ENABLED=true` and set `REDIRECT_BASE_URL` to that proxy's public origin
without a trailing slash. `GET /healthz` does not access the database.
