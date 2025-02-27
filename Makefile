SHELL := /bin/bash

export PATH := ./venv/bin:$(PATH)

SHA  := $(shell git rev-parse --short HEAD )
REPO := quay.io/quay/quay
TAG  := $(REPO):$(SHA)

MODIFIED_FILES_COUNT = $(shell git diff --name-only origin/master | grep -E .+\.py$ | wc -l)
GIT_MERGE_BASED = $(shell git merge-base origin/master HEAD)
MODIFIED_FILES = $(shell git diff --name-only $(GIT_MERGE_BASED) | grep -E .+\.py$ | paste -sd ' ')

show-modified:
	echo $(MODIFIED_FILES)

.PHONY: all unit-test registry-test registry-test-old buildman-test test build run clean

all: clean test build

QUAY_CONFIG ?= ../quay-config
conf/stack/license: $(QUAY_CONFIG)/local/license
	mkdir -p conf/stack
	ln -s $(QUAY_CONFIG)/local/license conf/stack/license

unit-test:
	TEST=true PYTHONPATH="." py.test \
	--cov="." --cov-report=html --cov-report=term-missing \
	--timeout=3600 --verbose -x \
	./

registry-test:
	TEST=true PYTHONPATH="." py.test  \
	--cov="." --cov-report=html --cov-report=term-missing \
	--timeout=3600 --verbose --show-count -x \
	test/registry/registry_tests.py


buildman-test:
	TEST=true PYTHONPATH="." py.test \
	--cov="." --cov-report=html --cov-report=term-missing \
	--timeout=3600 --verbose --show-count -x \
	./buildman/

certs-test:
	./test/test_certs_install.sh

full-db-test: ensure-test-db
	TEST=true PYTHONPATH=. QUAY_OVERRIDE_CONFIG='{"DATABASE_SECRET_KEY": "anothercrazykey!"}' \
	alembic upgrade head
	TEST=true PYTHONPATH=. \
	SKIP_DB_SCHEMA=true py.test --timeout=7200 \
	--verbose --show-count -x --ignore=endpoints/appr/test/ \
	./

clients-test:
	cd test/clients; python clients_test.py

types-test:
	mypy .

test: unit-test registry-test registry-test-old certs-test

ensure-test-db:
	@if [ -z $(TEST_DATABASE_URI) ]; then \
	  echo "TEST_DATABASE_URI is undefined"; \
	  exit 1; \
	fi

PG_PASSWORD := quay
PG_USER := quay
PG_HOST := postgresql://$(PG_USER):$(PG_PASSWORD)@localhost/quay

test_postgres : TEST_ENV := SKIP_DB_SCHEMA=true TEST=true \
	TEST_DATABASE_URI=$(PG_HOST) PYTHONPATH=.

test_postgres:
	docker rm -f postgres-testrunner-postgres || true
	docker run --name postgres-testrunner-postgres \
		-e POSTGRES_PASSWORD=$(PG_PASSWORD) -e POSTGRES_USER=${PG_USER} \
		-p 5432:5432 -d postgres:9.2
	until pg_isready -d $(PG_HOST); do sleep 1; echo "Waiting for postgres"; done
	$(TEST_ENV) alembic upgrade head
	$(TEST_ENV) py.test --timeout=7200 --verbose --show-count ./ --color=no \
		--ignore=endpoints/appr/test/ -x
	docker rm -f postgres-testrunner-postgres || true

WEBPACK := node_modules/.bin/webpack
$(WEBPACK): package.json
	npm install webpack
	npm install

BUNDLE := static/js/build/bundle.js
$(BUNDLE): $(WEBPACK) tsconfig.json webpack.config.js typings.json
	$(WEBPACK)

GRUNT := grunt/node_modules/.bin/grunt
$(GRUNT): grunt/package.json
	cd grunt && npm install

JS  := quay-frontend.js quay-frontend.min.js template-cache.js
CSS := quay-frontend.css
DIST := $(addprefix static/dist/, $(JS) $(CSS) cachebusters.json)
$(DIST): $(GRUNT)
	cd grunt && ../$(GRUNT)

build: $(WEBPACK) $(GRUNT)

docker-build: build
	ifneq (0,$(shell git status --porcelain | awk 'BEGIN {print $N}'))
	echo 'dirty build not supported - run `FORCE=true make clean` to remove'
	exit 1
	endif
	# get named head (ex: branch, tag, etc..)
	NAME = $(shell git rev-parse --abbrev-ref HEAD)
	# checkout commit so .git/HEAD points to full sha (used in Dockerfile)
	git checkout $(SHA)
	docker build -t $(TAG) .
	git checkout $(NAME)
	echo $(TAG)

app-sre-docker-build:
	$(BUILD_CMD) -t ${IMG} -f Dockerfile.deploy .

run: license
	goreman start


clean:
	find . -name "*.pyc" -exec rm -rf {} \;
	rm -rf node_modules 2> /dev/null
	rm -rf grunt/node_modules 2> /dev/null
	rm -rf dest 2> /dev/null
	rm -rf dist 2> /dev/null
	rm -rf .cache 2> /dev/null
	rm -rf static/js/build
	rm -rf static/build
	rm -rf static/dist
	rm -rf build
	rm -rf conf/stack
	rm -rf screenshots


yapf-all:
	yapf -r . -p -i


yapf-diff:
	if [ $(MODIFIED_FILES_COUNT) -ne 0 ]; then yapf -d -p $(MODIFIED_FILES) ; fi


yapf-test:
	if [ `yapf -d -p $(MODIFIED_FILES) | wc -l` -gt 0 ] ; then false ; else true ;fi


generate-proto-py:
	python -m grpc_tools.protoc -Ibuildman/buildman_pb --python_out=buildman/buildman_pb --grpc_python_out=buildman/buildman_pb buildman.proto


black:
	black --line-length=100 --target-version=py38 --exclude "/(\.eggs|\.git|\.hg|\.mypy_cache|\.nox|\.tox|\.venv|_build|buck-out|build|dist)/" .

#################################
# Local Development Environment #
#################################

.PHONY: local-dev-clean
local-dev-clean:
	./local-dev/scripts/clean.sh

.PHONY: local-dev-build-frontend
local-dev-build-frontend:
	make local-dev-clean
	npm install --quiet --no-progress --ignore-engines --no-save
	npm run --quiet build

.PHONY: local-dev-build
local-dev-build:
	make local-dev-clean
	make local-dev-build-frontend
	docker-compose build

.PHONY: local-dev-up
local-dev-up:
	make local-dev-clean
	make local-dev-build-frontend
	docker-compose up -d redis
	docker-compose up -d quay-db
	docker exec -it quay-db bash -c 'while ! pg_isready; do echo "waiting for postgres"; sleep 2; done'
	docker-compose up -d quay

local-docker-rebuild:
	docker-compose up -d redis --build
	docker-compose up -d quay-db --build
	docker exec -it quay-db bash -c 'while ! pg_isready; do echo "waiting for postgres"; sleep 2; done'
	docker-compose up -d quay --build
	docker-compose restart quay

ifeq ($(CLAIR),true)
	docker-compose up -d clair-db --build
	docker exec -it clair-db bash -c 'while ! pg_isready; do echo "waiting for postgres"; sleep 2; done'
	docker-compose up -d clair --build
	docker-compose restart clair
else
	@echo "Skipping Clair"
endif


.PHONY: local-dev-up-with-clair
local-dev-up-with-clair:
	make local-dev-clean
	make local-dev-build-frontend
	docker-compose up -d redis
	docker-compose up -d quay-db
	docker exec -it quay-db bash -c 'while ! pg_isready; do echo "waiting for postgres"; sleep 2; done'
	docker-compose up -d quay
	docker-compose up -d clair-db
	docker exec -it clair-db bash -c 'while ! pg_isready; do echo "waiting for postgres"; sleep 2; done'
	docker-compose up -d clair

.PHONY: local-dev-up
local-dev-down:
	docker-compose down
	make local-dev-clean
