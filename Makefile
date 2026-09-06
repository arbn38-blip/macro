.PHONY: test run smoke
test:
	cd collector && .venv/bin/pytest -v
run:
	docker compose up --build
smoke:
	./scripts/smoke.sh
