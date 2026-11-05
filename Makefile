# Botville — server stack entry points (A0.2).
#
#   make up              # core one-button start (synapse + postgres)
#   make up HS=tuwunel   # same for tuwunel / continuwuity
#   make up PUSH=1       # also start the sygnal push gateway (A1.2), any HS;
#                        # force-recreates ONLY sygnal so freshly rendered
#                        # APNs config (APNS_PLATFORM / BOTVILLE_APNS_ENABLED /
#                        # ...) is picked up by the running gateway (PR #127 B1)
#   make smoke           # run server/tests/smoke.py against the running stack
#   make backup          # dump synapse DB + data volume into ./backups/<ts>/ (A1.1)
#   make restore RESTORE_DIR=backups/<ts>   # destructive restore of a backup (A1.1)
#   make e2e             # register/chat/group acceptance test; E2E_PHASE=seed|verify (A1.1)
#   make push-e2e        # HS -> sygnal push latency acceptance (<5s, A1.2);
#                        # needs the stack up with PUSH=1 (mock pushkin, no APNs)
#   make appservice-e2e  # appservice acceptance: gateway sends as an @ai_* user (A1.3)
#   make federation-e2e  # paired-federation acceptance (A2.1): independent
#                        # botville-fed compose project with two Synapse
#                        # instances behind fedgate/pairing — unpaired deny,
#                        # paired invite/join/messages, sever both ways
#   make federation-down # tear down (-v) only the botville-fed project
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

# Optional push gateway (A1.2): PUSH=1 (also true/yes) on up/logs/ps adds
# --profile push (sygnal; stacks onto any HS profile). `down` always carries
# --profile push so a PUSH=1 stack tears down fully (sygnal and the compose
# network would otherwise linger; idempotent when push was never started —
# PUSH=0 does not opt out of `down`). Loopback port mirrors
# BOTVILLE_PUSH_PORT; homeservers reach sygnal in-network at sygnal:5000.
# `up` with PUSH=1 additionally force-recreates ONLY the sygnal container
# after `up -d` (PR #127 B1): the APNs-related .env keys are consumed by
# init.sh when RENDERING the bind-mounted generated/sygnal/sygnal.yaml, not
# by compose interpolation — the service config hash never changes, `up -d`
# alone leaves the old container running, and sygnal parses its config only
# at startup. Without PUSH, `up` never touches sygnal.
PUSH ?=
PUSH_PROFILE := $(if $(filter 1 true yes,$(PUSH)),--profile push,)
PUSH_PORT ?= $(shell sed -n 's/^BOTVILLE_PUSH_PORT=//p' $(ENV_FILE) 2>/dev/null | tail -n 1)
ifeq ($(strip $(PUSH_PORT)),)
PUSH_PORT := 8019
endif

# Backup/restore/e2e knobs (A1.1).
E2E_PHASE ?= seed
E2E_STATE ?= backups/e2e-state.json

.PHONY: init up down logs smoke ps backup restore e2e push-e2e appservice-e2e \
	federation-e2e federation-down

init:
	bash server/scripts/init.sh

# Core one-button startup: bootstrap .env + rendered synapse config, then
# start the selected homeserver profile detached (plus the push gateway with
# PUSH=1). PUSH=1 additionally force-recreates ONLY the sygnal container
# after `up -d` (PR #127 B1): APNs-related .env keys (APNS_PLATFORM,
# BOTVILLE_APNS_ENABLED, ...) only change init.sh's rendering of the
# bind-mounted generated/sygnal/sygnal.yaml — compose interpolation never
# sees them, so `up -d` alone leaves the old container (and its
# startup-parsed config) running. --force-recreate --no-deps reloads the
# config without touching the homeserver/postgres. Without PUSH, up never
# touches sygnal. continuwuity additionally registers its first-boot admin
# account (one-time token from the container logs; see
# server/scripts/bootstrap-continuwuity.sh).
up:
	bash server/scripts/init.sh
	$(COMPOSE) --profile $(HS) $(PUSH_PROFILE) up -d
ifneq ($(PUSH_PROFILE),)
	$(COMPOSE) --profile $(HS) --profile push up -d --force-recreate --no-deps sygnal
endif
ifeq ($(HS),continuwuity)
	COMPOSE='$(COMPOSE)' bash server/scripts/bootstrap-continuwuity.sh
endif

down:
	$(COMPOSE) --profile $(HS) --profile push down --remove-orphans

logs:
	$(COMPOSE) --profile $(HS) $(PUSH_PROFILE) logs -f --tail=200

ps:
	$(COMPOSE) --profile $(HS) $(PUSH_PROFILE) ps

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

# Push gateway acceptance (A1.2, issue #4): register alice/bob, bob sets an
# http pusher for the org.botville.mock pushkin, alice sends a DM, assert the
# notification reaches sygnal within E2E_MAX_PUSH_LATENCY seconds. Requires
# the stack up with PUSH=1 (make up HS=<hs> PUSH=1). Covers HS -> sygnal
# only; real APNs delivery needs a real .p8 + device (docs/deploy/push-sygnal.md).
E2E_MAX_PUSH_LATENCY ?= 5
push-e2e: init
	python3 server/tests/push_e2e.py \
		--base-url http://127.0.0.1:$(PORT) \
		--registration-token "$$(sed -n 's/^BOTVILLE_REGISTRATION_TOKEN=//p' $(ENV_FILE) | tail -n 1)" \
		--sygnal-base-url http://127.0.0.1:$(PUSH_PORT) \
		--max-latency $(E2E_MAX_PUSH_LATENCY) \
		--pushes-cmd '$(COMPOSE) --profile push exec -T sygnal cat /data/mock-pushes.jsonl'

# --- appservice acceptance (A1.3, synapse + tuwunel) -----------------------
# Gateway-side acceptance: the homeserver runs with the botville-gateway
# appservice registered (init.sh renders it from .env), and the real gateway
# transport (gateway/src) registers an @ai_* user, joins an invited room and
# delivers as that identity; exclusive-namespace rejections asserted both
# ways. continuwuity is not covered yet (no file-based registration) — see
# docs/deploy/appservice.md.
appservice-e2e: init
	python3 server/tests/appservice_e2e.py \
		--base-url http://127.0.0.1:$(PORT) \
		--env-file $(ENV_FILE)

# --- paired federation (A2.1, independent compose project) -----------------
# botville-fed runs TWO Synapse instances (hs-a/hs-b), each behind its own
# fedgate gate + pairing service (server/pairing, stdlib-only python
# containers). docker-compose.federation.yml is standalone: it never
# touches the main stack's project, network or volumes, and only maps
# loopback test ports (18101/18102 client, 18111/18112 pairing admin).
# Certs/configs/admin tokens are generated at run time into
# server/compose/federation/generated/ (gitignored). CI-only Synapse
# relaxations (federation_verify_certificates=false, private-range
# blacklist cleared, open registration) live in the generated configs —
# see docs/deploy/paired-federation.md.
FED_DIR := server/compose/federation
FED_COMPOSE_FILE := $(FED_DIR)/docker-compose.federation.yml
FED_ENV := $(FED_DIR)/generated/federation.env
FED_COMPOSE := docker compose -p botville-fed -f $(FED_COMPOSE_FILE) --env-file $(FED_ENV)

federation-e2e:
	bash server/scripts/federation-certs.sh
	python3 server/scripts/federation-setup.py
	$(FED_COMPOSE) up -d
	python3 server/tests/federation_e2e.py --env-file $(FED_ENV)

federation-down:
	# down -v needs the env file for interpolation; regenerate it if this
	# checkout never ran federation-e2e (cheap, no Docker involved).
	@test -f "$(FED_ENV)" || python3 server/scripts/federation-setup.py
	$(FED_COMPOSE) down -v --remove-orphans
