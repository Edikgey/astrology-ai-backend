# Birth time and house accuracy correction (local only)

## Root causes and changes

1. `TryFreePage` displayed minute selection but submitted only `parseInt(hour)`.
   Its separate Julian-date helper looked up Russian month labels in an English
   month list and its result was ignored by the backend. The helper is removed.
   The API now receives hour + minute and stores local date + decimal local hour
   (e.g. 12:37 = 12 + 37/60), retaining compatibility with MyCharts HH:MM rendering.
   Decimal-hour API clients remain supported when supplying a timezone and omitting
   the separate minute field. Invalid dates/times are rejected before calculation.

2. The existing OpenCage response includes `annotations.timezone.name`, but the UI
   discarded it, and no timezone/UTC instant was stored. Local date/hour was passed
   unchanged to `swe.julday`/`calc_ut`, which interpret the input as UTC.
   The selected geocoder result now carries its IANA timezone. Editing birthplace
   invalidates coordinates/timezone; stale geocoder responses cannot restore them.
   Zero latitude/longitude are preserved. Browser payload/header debug logging was
   removed. The geocoder's current numeric offset is deliberately not used for birth.
   `modules/birth_time.py` resolves local time using Python ZoneInfo and the birth date,
   including historical DST and date/month/year rollover. Timezone and a frozen naive
   UTC timestamp are stored, keeping UTC convention consistent with the existing DB.
   All calculator entry points use this UTC instant rather than converting again.
   No browser-timezone inference or new external timezone service is introduced.

3. `get_house_cusps()` used Swiss Ephemeris `A` (Equal), while placements and axes used
   `P` (Placidus). Placements also used formatted/rounded within-sign degrees, causing
   near-cusp errors. `HOUSE_SYSTEM_CODE=P` now applies throughout; display cusps come
   from `get_houses()` without rounding. Planet/asteroid house assignment uses exact
   absolute longitude and normalized circular intervals. A 1e-10-degree boundary
   tolerance handles float modulo artifacts at a cusp. The old `get_planet_houses`
   helper now reuses the calculated house instead of calling undefined `find_house`.
   Two directly related longitude defects were corrected: zero longitude was discarded
   by `or`, and Lilith/Selena stored the last JPL asteroid longitude instead of their own
   (also crashing when no JPL result existed). Their saved longitude and house now agree.

## DST, legacy data and AI

Nonexistent clock times during a forward transition return 422 with a clear message.
Repeated times during a backward transition require explicit `time_fold=0/1`; the form
shows a small UTC-offset selector only when needed. There is no silent DST choice.
New requests without a valid timezone are rejected; users must select a geocoded place.
Geocoder result selection determines the zone; historical offsets come from the runtime's
IANA database, not the current geocoder offset. Historical boundary changes and timezone
database precision remain limits of the underlying location/timezone datasets.

New `ChartData` stores houses and house-system name in the same transaction as placements.
Refresh reads the saved cusp snapshot. AI context reads saved placements/cusps/timezone/
UTC data and performs no astrology calculations. A legacy chart without UTC/timezone
keeps the former calculation instant for existing calculator routes and is explicitly
marked `legacy_unverified`. The chart view may reconstruct Placidus cusps using those
legacy inputs, without rewriting the DB. AI omits absent cusp data instead of computing
it. Lost original minutes/timezone cannot be reliably recovered automatically; no old
birth inputs, chart snapshots, messages, memory or quotas are backfilled or reset.
Legacy charts should be recreated from verified birth inputs when accurate interpretation
is required. No claim is made that unknown legacy inputs have been corrected retroactively.

## SQL and changed files

`birth_time_houses.sql` adds nullable `natal_charts.timezone`, `birth_utc` and
`chart_data.houses`, `house_system`. Apply once before running this corrected backend
against an existing DB (along with the preceding AI migration for the pending AI release).
The SQL was applied only in isolated local PostgreSQL test schemas, not production.

Backend files changed for this follow-up:
`api/endpoints.py`, `database/queries.py`, `models/natal_chart.py`, `modules/ephemeris.py`,
`modules/ai_chart_context.py`, new `modules/birth_time.py`,
`migrations/birth_time_houses.sql`, this report,
`tests/test_birth_time.py`, `tests/test_my_charts.py` (fixture timezone),
`tests/test_ai_conversation.py` (saved-cusp fixture), and `tests/test_postgres_usage.py`.
Frontend: `src/pages/TryFreePage.js`, new `src/pages/TryFreePage.test.js`.
Prior AI work remains in the working tree. No Paddle/usage/memory implementation changed
in this follow-up. No dependency changes, UI redesign, push or deployment.

## Validation

- Full backend suite: **95/95 passed**, no skips, including **9 PostgreSQL checks**.
- Frontend: **75/75 tests passed**, 7 suites; production build successful.
- Local PostgreSQL 18.6 stopped after tests. No real OpenAI/geocoder/JPL test requests.
- Tests cover full minute/zone API-to-DB pipeline, UTC midnight/year rollover,
  summer/winter offsets, ambiguous/nonexistent clock times, invalid zones/dates,
  decimal-hour compatibility, legacy null metadata, exact Placidus cusps and boundary
  membership, precision near cusps, zero longitude, synthetic-point longitude ownership,
  saved snapshot refresh, AI with calculation disabled, migration preservation/repetition,
  zero coordinates, stale place suggestions, and the conditional DST selector.
- Existing Pydantic orm_mode and Browserslist age warnings remain.

Sources checked: [OpenCage timezone annotations](https://opencagedata.com/guides/how-to-find-the-time-zone-for-an-address-or-coordinates),
[Python ZoneInfo](https://docs.python.org/3/library/zoneinfo.html),
[Swiss Ephemeris house-system codes](https://www.astro.com/swisseph/swephprg.htm).
