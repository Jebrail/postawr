EXTENSION = pg_awr
DATA = sql/pg_awr--1.0.sql sql/pg_awr--1.0--1.1.sql sql/pg_awr--1.2.sql sql/pg_awr--1.1--1.2.sql
DATA_built = sql/pg_awr--1.1.sql
SCRIPTS = bin/pg-awr-job
PG_CONFIG ?= pg_config
# Binary packages contain only SQL and a shell helper; PGXS is not needed to package them.
ifneq ($(strip $(MAKECMDGOALS)),)
PACKAGE_ONLY = $(if $(filter-out package-deb package-rpm packages,$(MAKECMDGOALS)),,yes)
endif
ifneq ($(PACKAGE_ONLY),yes)
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
endif
PG_MAJOR ?= 18

sql/pg_awr--1.1.sql: sql/pg_awr--1.0.sql sql/pg_awr--1.0--1.1.sql
	cat $^ > $@

.PHONY: test test-legacy package-deb package-rpm packages
test: all
	python3 tests/native-extension.py $(TEST_ARGS)

test-legacy: all
	python3 tests/integration.py $(TEST_ARGS)

package-deb:
	python3 packaging/build.py deb --pg-major $(PG_MAJOR)

package-rpm:
	python3 packaging/build.py rpm --pg-major $(PG_MAJOR)

packages: package-deb package-rpm
