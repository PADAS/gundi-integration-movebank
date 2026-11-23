# WD Mara Raptor study: EarthRanger coverage audit (2026-11-22)

Per Movebank individual (study 170240210, 60 with data): first Movebank record vs. the earliest
observation on its EarthRanger source (wildlifedynamics.pamdas.org, matched by manufacturer_id),
plus month-by-month presence between the Movebank first and last record. Movebank event counts
cover all sensor types. Source: `/tmp/wd-audit/er_audit2.py` run with the Gundi service account
token; Movebank individuals from the integration's service account.

**Result: 12 of 60 birds are covered from their first record; 48 are not** (140,690,540 Movebank events across them, vs 19,059,459 for the covered ones).

Caveat: an empty month for a covered bird may be a genuine transmission gap in Movebank rather than
missing data; only a Movebank-side month count would separate the two.

| bird | ER status | Movebank first | ER earliest | days missing at start | Movebank events | ER total (if known) | empty months |
|---|---|---|---|---|---|---|---|
| Tawny Eagle 04 | GAPS | 2019-11-22 | 2025-10-09 | 2148 | 14,226,366 | 13,411 | 56 (2019-11..2024-06) |
| Tawny Eagle 03 | GAPS | 2019-11-21 | 2025-09-04 | 2114 | 14,367,165 | 22,483 | 64 (2019-11..2025-02) |
| Martial Eagle 18 | GAPS | 2017-02-08 | 2022-03-19 | 1865 | 903,877 | 321,664 | 62 (2017-02..2025-09) |
| Martial Eagle 12 | GAPS | 2016-12-10 | 2022-01-01 | 1848 | 789,172 | 278,648 | 61 (2016-12..2021-12) |
| Martial Eagle 13 | GAPS | 2016-12-11 | 2021-12-12 | 1827 | 385,595 | 96,267 | 173 (2016-12..2033-02) |
| Tawny Eagle 07 | GAPS | 2020-12-12 | 2025-10-09 | 1762 | 4,921,938 | - | 58 (2020-12..2025-09) |
| Martial Eagle 10 | GAPS | 2016-11-30 | 2021-09-01 | 1736 | 388,038 | - | 58 (2016-11..2021-08) |
| Martial Eagle 11 | GAPS | 2016-12-08 | 2021-07-10 | 1675 | 1,087,637 | - | 58 (2016-12..2025-09) |
| Martial Eagle 15 | GAPS | 2017-01-30 | 2021-09-01 | 1675 | 392,982 | - | 56 (2017-01..2021-08) |
| Tawny Eagle 05 | GAPS | 2020-09-15 | 2025-03-27 | 1654 | 16,352,458 | - | 47 (2020-09..2024-07) |
| Tawny Eagle 13 | GAPS | 2021-01-08 | 2025-06-28 | 1632 | 5,315,309 | - | 53 (2021-01..2025-05) |
| Martial Eagle 39 | GAPS | 2021-09-23 | 2026-03-01 | 1620 | 1,126,010 | - | 54 (2021-09..2026-02) |
| Tawny Eagle 10 | GAPS | 2020-12-20 | 2025-05-17 | 1609 | 5,130,621 | - | 53 (2020-12..2025-04) |
| Martial Eagle 06 | GAPS | 2016-11-10 | 2021-03-20 | 1591 | 830,946 | - | 52 (2016-11..2021-02) |
| Martial Eagle 22 | GAPS | 2017-08-30 | 2022-01-01 | 1585 | 74,060 | - | 59 (2017-08..2024-01) |
| Tawny Eagle 02 | GAPS | 2019-10-13 | 2024-01-07 | 1547 | 15,870,089 | - | 51 (2019-10..2023-12) |
| Martial Eagle 31 | GAPS | 2020-12-14 | 2025-03-01 | 1538 | 3,185,346 | - | 51 (2020-12..2025-02) |
| Martial Eagle 21 | GAPS | 2017-07-11 | 2021-09-01 | 1513 | 498,441 | - | 50 (2017-07..2021-08) |
| Tawny Eagle 14 | GAPS | 2021-01-10 | 2024-11-05 | 1395 | 6,454,619 | 46,202 | 43 (2021-01..2024-07) |
| Martial Eagle 23 | GAPS | 2018-12-17 | 2022-04-09 | 1209 | 983,204 | - | 40 (2018-12..2022-03) |
| Martial Eagle 26 | GAPS | 2019-10-09 | 2022-10-15 | 1102 | 1,186,373 | - | 36 (2019-10..2022-09) |
| Tawny Eagle 11 | GAPS | 2020-12-18 | 2023-12-24 | 1101 | 2,641,120 | - | 36 (2020-12..2023-11) |
| Martial Eagle 24 | GAPS | 2019-04-22 | 2022-03-03 | 1046 | 653,066 | - | 38 (2019-04..2025-09) |
| Martial Eagle 35 | GAPS | 2021-09-18 | 2024-05-26 | 981 | 1,082,716 | - | 35 (2021-09..2026-06) |
| Martial Eagle 27 | GAPS | 2019-10-09 | 2022-04-20 | 924 | 237,675 | 59,906 | 30 (2019-10..2022-03) |
| Tawny Eagle 09 | GAPS | 2020-12-15 | 2023-06-01 | 898 | 5,588,072 | 164,460 | 30 (2020-12..2023-05) |
| Tawny Eagle 12 | GAPS | 2020-12-21 | 2023-05-19 | 879 | 3,477,774 | - | 29 (2020-12..2023-04) |
| Tawny Eagle 06 | GAPS | 2020-12-10 | 2023-04-25 | 866 | 3,438,818 | 18,098 | 28 (2020-12..2023-03) |
| Martial Eagle 25 | GAPS | 2019-04-23 | 2021-09-01 | 862 | 396,512 | - | 29 (2019-04..2021-08) |
| Martial Eagle 08 | GAPS | 2016-12-06 | 2019-03-18 | 832 | 472,606 | - | 32 (2016-12..2025-11) |
| Martial Eagle 01 | GAPS | 2016-07-15 | 2018-10-06 | 813 | 107,165 | 36,549 | 27 (2016-07..2018-09) |
| African Hawk-eagle 01 (OMC) | GAPS | 2019-03-25 | 2021-05-11 | 778 | 458,995 | - | 27 (2019-03..2025-01) |
| Martial Eagle 07 | GAPS | 2016-09-20 | 2018-11-03 | 774 | 256,395 | 9,241 | 26 (2016-09..2018-10) |
| Tawny Eagle 08 | GAPS | 2020-12-15 | 2023-01-01 | 747 | 4,766,861 | - | 25 (2020-12..2022-12) |
| Martial Eagle 34 | GAPS | 2021-04-29 | 2023-03-03 | 673 | 1,709,900 | 18,326 | 23 (2021-04..2023-02) |
| Tawny Eagle 16 | GAPS | 2021-01-11 | 2022-09-26 | 623 | 5,908,448 | 1,529,219 | 20 (2021-01..2022-08) |
| Tawny Eagle 15 | GAPS | 2021-01-10 | 2022-06-13 | 519 | 2,564,369 | 107,828 | 17 (2021-01..2022-05) |
| Martial Eagle 05 | GAPS | 2016-09-15 | 2018-01-03 | 475 | 141,238 | 50,351 | 16 (2016-09..2017-12) |
| Martial Eagle 29 | GAPS | 2020-09-24 | 2021-11-29 | 431 | 4,546,319 | - | 14 (2020-09..2021-10) |
| Martial Eagle 32 | GAPS | 2020-12-20 | 2022-01-18 | 394 | 3,061,541 | - | 13 (2020-12..2021-12) |
| Martial Eagle 38 | GAPS | 2021-09-23 | 2022-10-02 | 374 | 982,407 | - | 13 (2021-09..2022-09) |
| Steppe Eagle 01 | GAPS | 2020-12-16 | 2021-11-12 | 331 | 504,170 | - | 12 (2020-12..2025-08) |
| Martial Eagle 37 | GAPS | 2021-09-21 | 2022-06-30 | 282 | 1,297,617 | - | 20 (2021-09..2023-06) |
| Martial Eagle 30 | GAPS | 2020-12-11 | 2021-09-01 | 264 | 247,665 | - | 9 (2020-12..2021-08) |
| Martial Eagle 36 | GAPS | 2021-09-19 | 2022-05-03 | 226 | 898,470 | - | 8 (2021-09..2022-04) |
| African Hawk-eagle 02 (OMC) | GAPS | 2019-03-25 | 2019-07-17 | 114 | 75,622 | - | 4 (2019-03..2019-06) |
| Martial Eagle 17 | GAPS | 2017-02-08 | 2017-02-08 | 0 | 270,734 | - | 5 (2020-03..2020-08) |
| Martial Eagle 20 | GAPS | 2017-04-12 | 2017-04-12 | 0 | 434,019 | - | 8 (2018-08..2019-03) |
| Martial Eagle 03 | OK | 2016-11-08 | 2016-11-15 | 7 | 57,429 | - | 0 |
| African Hawk-eagle 03 (Talek) | OK | 2019-10-10 | 2019-10-10 | 0 | 59,350 | - | 0 |
| Martial Eagle 02 | OK | 2016-07-14 | 2016-07-14 | 0 | 27,336 | - | 0 |
| Martial Eagle 04 | OK | 2016-11-06 | 2016-11-06 | 0 | 110,483 | - | 0 |
| Martial Eagle 09 | OK | 2016-11-26 | 2016-11-26 | 0 | 18,215 | - | 0 |
| Martial Eagle 14 | OK | 2017-01-28 | 2017-01-28 | 0 | 339,931 | - | 0 |
| Martial Eagle 16 | OK | 2017-02-01 | 2017-02-01 | 0 | 208,790 | - | 0 |
| Martial Eagle 19 | OK | 2017-01-25 | 2017-01-25 | 0 | 190,448 | - | 0 |
| Martial Eagle 28 | OK | 2020-09-22 | 2020-09-22 | 0 | 1,551,049 | - | 0 |
| Martial Eagle 33 | OK | 2021-04-28 | 2021-04-28 | 0 | 740,952 | - | 0 |
| Steppe Eagle 02 | OK | 2021-01-08 | 2021-01-08 | 0 | 901,395 | - | 0 |
| Tawny Eagle 01 | OK | 2019-10-07 | 2019-10-07 | 0 | 14,854,081 | - | 0 |

## Observations

- Five birds (ME10, ME15, ME21, ME25, ME30) start in ER at 2021-09-01 00:30–03:41Z: a previous load with a 2021-09-01 start.
- Several big Tawny Eagles (TE03, TE04, TE14) have only tens of thousands of recent points from the scheduled pull against 5–14M Movebank events.
- The 12 covered birds are the 11 remediated in November plus ME02, ME03 (7 days short) and AHE03; ME17 and ME20 are covered from their first record but show empty months mid-deployment.
- Remaining backfill at the single-chain rate seen in November (~3.5M observations/day): roughly 40 chain-days; parallel chains divide that but share Movebank's 25-connection budget and the runner's 4 instances.
