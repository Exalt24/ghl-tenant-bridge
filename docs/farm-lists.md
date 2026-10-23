# Farm lists

*GPS points and polygons in, a status and a reason per plot out, then hectares of overlap against a reference layer.*

`supabase/migrations/0005_farm_lists.sql` adds `ingest_farm_list(org, name, geojson)` and `plot_overlaps(list, layer)`. A customer's file is a GeoJSON FeatureCollection of Point, Polygon and MultiPolygon plots. PostGIS accepts a ring that was never closed, a latitude of 200 and coordinates typed the wrong way round, and each would flow silently into an intersection, so every plot is stored with `accepted`, `repaired` or `rejected` and a reason code, with its original GeoJSON kept.

## Reason codes

| Code | Meaning |
|---|---|
| `unclosed_ring`, `too_few_points`, `self_intersection`, `hole_outside_shell`, `nested_holes` | PostGIS's own validity reason, mapped to a stable code (`farm_plot_reason`) so a caller never parses prose |
| `latitude_out_of_range`, `longitude_out_of_range` | a coordinate outside -90..90 or -180..180 |
| `coordinates_swapped`, `outside_country_box` | with an optional country box: outside it only when swapped, or outside it either way |
| `duplicate_plot_ref`, `unsupported_geometry`, `invalid_geojson`, `zero_area` | the plot id was already used in the file; a LineString or other type; geometry that does not parse; an area of about zero |
| `point_only` | a Point plot: accepted, no area |
| `over_area_ceiling` | a polygon over `p_max_ha` (default 1000): flagged, not rejected |

## Decisions worth reading

- **A rejected plot is kept, not dropped**, and a file that is not a FeatureCollection or has no features is refused as a whole, never read as an empty success.
- **The same file twice returns the same list** and adds no rows, keyed on the hash of the file.
- **A bow-tie ring is rejected, not repaired.** Its own area is zero while `ST_MakeValid` returns two triangles, so repairing it would turn one mistake into two plots. Only a defect whose repaired polygon keeps the original area within 1 percent (a spike) is marked `repaired`.
- **Touching is not overlapping.** Two squares that share an edge intersect in a line of area zero, and `plot_overlaps` does not report them. Areas are on geography, so the hectares are real: a 1 km square near the equator is 99.3 ha.
- **Tenant isolation is in the database.** Forced row-level security, a composite key so a plot can only join a list of its own org, and a test that tenant B can neither read nor write A's lists.

## Measured

On 2026-10-24 against `postgis/postgis:17-3.5`: `python tests/farm_lists.test.py` runs 61 checks, including a generated table of every defect crossed with every geometry type, and `python tests/farm_lists_mutants.py` removes 17 guards one at a time and all 17 are caught. The hectare figures are compared with a spherical formula computed in Python, not with PostGIS itself.

## What it is not

The reference regions in the tests are squares drawn by hand. No real deforestation dataset and no real farm list was used, nothing is deployed, point plots are accepted and flagged `point_only` with no area, and the rules are a validation sketch, not legal advice on any regulation.
