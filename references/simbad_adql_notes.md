# SIMBAD TAP ADQL Reference Notes

Endpoint: `https://simbad.cds.unistra.fr/simbad/sim-tap/sync` (with official mirrors `https://simbad.cfa.harvard.edu/simbad/sim-tap/sync` and `https://simbad.idia.ac.za/simbad/sim-tap/sync`, or override via `SIMBAD_TAP_URL`).

## Core Tables Used

- `basic`: Primary astronomical object table.
  - `oid`: Internal unique object identifier.
  - `main_id`: Primary SIMBAD identifier.
  - `ra`, `dec`: ICRS coordinates in decimal degrees (J2000).
  - `otype`: Object type code (e.g., `YSO`, `HII`, `PSR`, `SNR`, `AGN`, `*`, `Rad`, `X`).
- `ident`: All known identifiers / aliases for each object.
  - `oidref`: Foreign key referencing `basic.oid`.
  - `id`: Identifier string (e.g., `2MASS J...`, `Gaia DR3 ...`, `IRAS ...`).
- `has_ref` + `ref`: Links objects to bibliographic references (`has_ref.oidbibref = ref.oidbib`).
  - `has_ref.oidref`: Foreign key referencing `basic.oid`.
  - `has_ref.oidbibref`: Foreign key referencing `ref.oidbib`.
  - `ref.bibcode`: 19-character ADS/SciXplorer bibcode.

## Cone Search ADQL Pattern

```sql
SELECT TOP {limit}
  b.oid,
  b.main_id,
  b.otype,
  b.ra,
  b.dec,
  DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', {ra_deg}, {dec_deg})) * 3600.0 AS sep_arcsec
FROM basic AS b
WHERE CONTAINS(
  POINT('ICRS', b.ra, b.dec),
  CIRCLE('ICRS', {ra_deg}, {dec_deg}, {radius_deg})
) = 1
ORDER BY sep_arcsec ASC
```
