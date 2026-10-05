export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55209/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8209

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m fp.seed --if-empty
dev:            ## API with reload on :8209, jobs run by a local worker
	($(PY) -m core.jobs fp.api &) && $(PY) -m uvicorn fp.api:app --reload --port 8209
test:
	$(PY) -m pytest --cov=fp --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the demo scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first, then: make load-test SCENARIO=<scenario id> DATE=<a shoot date>
	$(PY) -m core.loadtest $(BASE) studio-crew-demo 32 10 "GET /v1/schedules/$(SCENARIO)" "GET /v1/call-sheets/$(DATE)?scenario_id=$(SCENARIO)"
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
