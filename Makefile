up:
	docker compose up -d

down:
	docker compose down

install:
	uv sync

run:
	uv run python -m engine version

lint:
	uv run ruff check . && uv run mypy engine delivery

fmt:
	uv run ruff format .

test:
	uv run pytest -q

db.migrate:
	uv run alembic upgrade head

db.downgrade:
	uv run alembic downgrade -1

db.revision:
	@test -n "$(name)" || (echo 'name is required: make db.revision name="message"' && exit 1)
	uv run alembic revision --autogenerate -m "$(name)"
