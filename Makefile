# AquaSentinel
#
# The short version:
#   make demo     run everything locally and open a dashboard with live data
#   make eval     measure the detectors and regenerate the results report
#   make test     run the test suite
#   make deploy   push the whole thing to AWS

PY ?= python3
PORT ?= 8080
SCENARIO ?= demo
SPEED ?= 600

.DEFAULT_GOAL := help
.PHONY: help demo run sim eval test lint deploy destroy synth provision clean check

help:
	@echo "AquaSentinel"
	@echo
	@echo "  make demo        local server + simulator + dashboard on :$(PORT)"
	@echo "                   (SCENARIO=$(SCENARIO) SPEED=$(SPEED))"
	@echo "  make run         local server only, no simulated data"
	@echo "  make sim         drive the virtual rig at an already-running server"
	@echo "  make eval        run every scenario through the engine and write"
	@echo "                   eval/results/{metrics.json,REPORT.md}"
	@echo "  make test        unit tests"
	@echo "  make check       test + eval, the gate before a demo"
	@echo "  make synth       render the CloudFormation without deploying"
	@echo "  make deploy      deploy to AWS (needs cdk bootstrap once)"
	@echo "  make provision   issue an IoT device certificate for a node"
	@echo "  make destroy     tear the AWS stack down"
	@echo
	@echo "  Scenarios: $$($(PY) -m sim.run --list 2>/dev/null | grep -v 'claim:' | awk '{print $$1}' | tr '\n' ' ')"

# A warm-up run first so the leak and demand modules know what normal looks
# like before anything is judged abnormal.
demo:
	$(PY) -m local.server --port $(PORT) --simulate $(SCENARIO) --speed $(SPEED) \
	  --warmup baseline_short

run:
	$(PY) -m local.server --port $(PORT)

sim:
	$(PY) -m sim.run --scenario $(SCENARIO) --transport http \
	  --url http://127.0.0.1:$(PORT) --speed $(SPEED)

eval:
	$(PY) eval/run_eval.py
	@echo
	@echo "--> eval/results/REPORT.md"

test:
	$(PY) -m unittest discover -s tests -v

check: test eval

lint:
	@$(PY) -m compileall -q engine sim services local eval tests && echo "compiles clean"

synth:
	cd infra && . .venv/bin/activate 2>/dev/null || true; \
	  cd $(CURDIR)/infra && npx --yes cdk@2 synth

deploy:
	cd infra && npx --yes cdk@2 deploy --require-approval never \
	  $(if $(ALERT_EMAIL),-c alert_email=$(ALERT_EMAIL),)

provision:
	./scripts/provision_device.sh $(if $(NODE),$(NODE),aquasentinel-node-01)

destroy:
	cd infra && npx --yes cdk@2 destroy --force

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
