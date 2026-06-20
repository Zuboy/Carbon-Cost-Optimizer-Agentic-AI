.PHONY: install lint format test deploy destroy synth agent clean

PYTHON   := python3
PROMPT  ?= "train a resnet50 model, deadline 6h, optimize for low carbon"

install:
	pip install -e ".[dev]"

lint:
	ruff check .
	black --check .

format:
	ruff check --fix .
	black .

test:
	pytest tests/ -v --cov=mcp_server --cov=agent --cov-report=term-missing

# CDK targets — requires Docker for Lambda bundling
synth:
	cdk synth

deploy:
	cdk deploy --require-approval never

destroy:
	cdk destroy --force

# Run the Strands agent locally (set MCP_SERVER_URL in .env first)
agent:
	$(PYTHON) -m agent.main --prompt $(PROMPT)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete 2>/dev/null; \
	rm -rf cdk.out/ .cdk.staging/ .coverage htmlcov/; \
	echo "Clean done"
