#!/bin/sh
# Mounted over the postgis image's own init scripts. They install PostGIS into the public schema and then call postgis_lib_version(), which fails
# once migration 0002 has put the extension in the extensions schema (the layout this repository uses, as Supabase does). Doing nothing here leaves
# the schema exactly as the migrations built it.
exit 0
