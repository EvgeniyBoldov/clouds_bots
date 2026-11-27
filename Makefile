RELEASE_ENV ?= release.env
.PHONY: release-preview release base-hash test test-delivery botx-emulator-up botx-send
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

botx-emulator-up:
	@docker compose build app
	@docker compose up -d --no-build --wait botx-emulator nginx
	@docker compose exec -T nginx nginx -s reload
	@bash -o pipefail -c 'docker compose --profile botx-emulator exec -T botx-emulator python -m gateway.botx_emulator credentials | docker compose exec -T app python -m gateway.botx_emulator register'

botx-send: export BOTX_EMULATOR_MESSAGE = $(value MESSAGE)
botx-send: export BOTX_EMULATOR_BOT_ID = $(BOT_ID)
botx-send: export BOTX_EMULATOR_CHAT_ID = $(CHAT_ID)
botx-send: export BOTX_EMULATOR_USER_HUID = $(USER_HUID)
botx-send: export BOTX_EMULATOR_CHAT_TYPE = $(CHAT_TYPE)
botx-send: export BOTX_EMULATOR_REPLY_TO = $(REPLY_TO)
botx-send:
	@docker compose --profile botx-emulator exec -T -e BOTX_EMULATOR_MESSAGE -e BOTX_EMULATOR_BOT_ID -e BOTX_EMULATOR_CHAT_ID -e BOTX_EMULATOR_USER_HUID -e BOTX_EMULATOR_CHAT_TYPE -e BOTX_EMULATOR_REPLY_TO botx-emulator python -m gateway.botx_emulator send
