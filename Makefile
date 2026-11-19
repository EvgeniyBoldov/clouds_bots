RELEASE_ENV ?= release.env
.PHONY: release-preview release base-hash test test-delivery
release-preview:
	@./scripts/release.sh preview $(RELEASE_ENV)
release:
	@./scripts/release.sh publish $(RELEASE_ENV)
base-hash:
	@bash -c 'source scripts/release-common.sh; base_input_sha'
test: test-delivery
	@python3 -m pytest -q tests
test-delivery:
	@bash -n scripts/*.sh
	@python3 -m unittest discover -s scripts/tests -q
