# Botville — server stack entry points (A0.2).
#
#   make up              # core one-button start (synapse + postgres)
#   make up HS=tuwunel   # same for tuwunel / continuwuity
#   make smoke           # run server/tests/smoke.py against the running stack
#
# Everything wraps docker compose on server/compose/docker-compose.yml with
# server/compose/.env (created by server/scripts/init.sh on first run).

COMPOSE_FILE := server/compose/docker-compose.yml
ENV_FILE := server/compose/.env
COMPOSE := docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE)

# Active homeserver profile: command line (make up HS=tuwunel) wins, then
# BOTVILLE_HOMESERVER from .env, then synapse.
HS ?= $(shell sed -n 's/^BOTVILLE_HOMESERVER=//p' $(ENV_FILE) 2>/dev/null | head -n 1)
ifeq ($(strip $(HS)),)
HS := synapse
endif

# Host port of the homeserver (mirrors BOTVILLE_HTTP_PORT).
PORT ?= $(shell sed -n 's/^BOTVILLE_HTTP_PORT=//p' $(ENV_FILE) 2>/dev/null | head -n 1)
ifeq ($(strip $(PORT)),)
PORT := 8008
endif

.PHONY: init up down logs smoke ps

init:
	bash server/scripts/init.sh

# Core one-button startup: bootstrap .env + rendered synapse config, then
# start the selected homeserver profile detached. continuwuity additionally
# registers its first-boot admin account (one-time token from the container
# logs; see server/scripts/bootstrap-continuwuity.sh).
up:
	bash server/scripts/init.sh
	$(COMPOSE) --profile $(HS) up -d
ifeq ($(HS),continuwuity)
	COMPOSE='$(COMPOSE)' bash server/scripts/bootstrap-continuwuity.sh
endif

down:
	$(COMPOSE) --profile $(HS) down --remove-orphans

logs:
	$(COMPOSE) --profile $(HS) logs -f --tail=200

ps:
	$(COMPOSE) --profile $(HS) ps

smoke: init
	python3 server/tests/smoke.py \
		--base-url http://127.0.0.1:$(PORT) \
		--registration-token "$$(sed -n 's/^BOTVILLE_REGISTRATION_TOKEN=//p' $(ENV_FILE))"
