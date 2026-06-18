.PHONY: deps lint start test build-and-deploy-to-staging

deps:
	uv sync

lint:
	uv run ruff format
	uv run ruff check --fix

start:
	uv run avni-autopilot

test:
	uv run pytest

build-and-deploy-to-staging:
	tar -czf /tmp/avni-autopilot.tar.gz \
		--exclude='.git' \
		--exclude='.venv' \
		--exclude='__pycache__' \
		--exclude='*.pyc' \
		--exclude='.pytest_cache' \
		--exclude='logs' \
		--exclude='resources/input' \
		--exclude='resources/output' \
		.
	scp /tmp/avni-autopilot.tar.gz avni-staging:/tmp/avni-autopilot.tar.gz
	ssh avni-staging "sudo tar -xzf /tmp/avni-autopilot.tar.gz -C /opt/avni-autopilot/current && sudo chown -R avni-autopilot-user:avni-autopilot-grp /opt/avni-autopilot/current && sudo systemctl reset-failed avni-autopilot; sudo systemctl restart avni-autopilot"
