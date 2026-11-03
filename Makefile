# Botville — server stack entry points (A0.2).
#
#   make up              # core one-button start (synapse + postgres)
#   make up HS=tuwunel   # same for tuwunel / continuwuity
#   make smoke           # run server/tests/smoke.py against the running stack
#   make backup          # dump synapse DB + data volume into ./backups/<ts>/ (A1.1)
#   make restore RESTORE_DIR=backups/<ts>   # destructive restore of a backup (A1.1)
#   make e2e             # register/chat/group acceptance test; E2E_PHASE=seed|verify (A1.1)
#
# Everything wraps docker compose on server/compose/docker-compose.yml with
# server/compose/.env (created by server/scripts/init.sh on first run).
#
# PROJECT pins the compose project name so multiple checkouts on one machine
# never share containers/volumes (down -v on one would wipe the other).
# Default: COMPOSE_PROJECT_NAME from .env (.env.example sets botville);
# override ad hoc with `make ps PROJECT=botville-zcode` etc. Every compose
# command below passes -p $(PROJECT) explicitly. Volumes are named
# <project>_synapse-data / <project>_postgres-data accordingly.

COMPOSE_FILE := server/compose/docker-compose.yml
ENV_FILE := server/compose/.env
PROJECT ?= $(shell sed -n 's/^COMPOSE_PROJECT_NAME=//p' $(ENV_FILE) 2>/dev/null | head -n 1)
ifeq ($(strip $(PROJECT)),)
PROJECT := botville
endif
COMPOSE := docker compose -p $(PROJECT) -f $(COMPOSE_FILE) --env-file $(ENV_FILE)

# Active homeserver profile: command line (make up HS=tuwunel) wins, then
# BOTVILLE_HOMESERVER from .env, then synapse. The last assignment of a key
# in .env wins, matching docker compose's --env-file handling.
HS ?= $(shell sed -n 's/^BOTVILLE_HOMESERVER=//p' $(ENV_FILE) 2>/dev/null | tail -n 1)
ifeq ($(strip $(HS)),)
HS := synapse
endif

# Host port of the homeserver (mirrors BOTVILLE_HTTP_PORT; last one wins).
PORT ?= $(shell sed -n 's/^BOTVILLE_HTTP_PORT=//p' $(ENV_FILE) 2>/dev/null | tail -n 1)
ifeq ($(strip $(PORT)),)
PORT := 8008
endif

# Backup/restore/e2e knobs (A1.1).
E2E_PHASE ?= seed
E2E_STATE ?= backups/e2e-state.json

.PHONY: init up down logs smoke ps backup restore e2e

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
		--registration-token "$$(sed -n 's/^BOTVILLE_REGISTRATION_TOKEN=//p' $(ENV_FILE) | tail -n 1)"

# --- backup / restore / e2e (A1.1, synapse profile) ----------------------
# Backup runs against the live stack; see server/scripts/backup-synapse.sh
# for what lands in ./backups/<timestamp>/ and how to override COMPOSE/BACKUP_DIR.
# Both scripts default to the same compose project as make (-p from .env).
backup:
	bash server/scripts/backup-synapse.sh

# Destructive: wipes the current DB + data volume and restores the backup.
# The Makefile target passes --yes; run the script directly to be prompted.
restore:
	@test -n "$(RESTORE_DIR)" || { echo "usage: make restore RESTORE_DIR=backups/<timestamp>"; exit 2; }
	bash server/scripts/restore-synapse.sh "$(RESTORE_DIR)" --yes

# E2E acceptance (Element-equivalent flows via the standard API):
#   make e2e                       # seed alice/bob/carol + DM + group, write state
#   make e2e E2E_PHASE=verify      # re-login after a backup/restore and re-check
# The state file holds passwords -> it lives under gitignored backups/.
# Depends on init so .env (and its registration token) always exists.
e2e: init
	python3 server/tests/e2e_synapse.py \
		--base-url http://127.0.0.1:$(PORT) \
		--registration-token "$$(sed -n 's/^BOTVILLE_REGISTRATION_TOKEN=//p' $(ENV_FILE) | tail -n 1)" \
		--phase $(E2E_PHASE) \
		--state $(E2E_STATE)
