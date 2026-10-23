#!/bin/sh
# Mounted over the postgis image's own init scripts. They install PostGIS into the public schema and then call postgis_lib_version(), which fails once
# migration 0002 has put the extension in the extensions schema (the layout this repository uses, as Supabase does). Doing nothing here leaves the schema
# exactly as the migrations built it.
#
# No `exit` on purpose: the postgres entrypoint SOURCES an init file that is not executable (git on Windows stores this one as mode 644), and a sourced
# `exit 0` ends the entrypoint itself, so the database container stops during first start. check_noop_init.py proves both halves.
:
