# Sex/gender side-by-side on one pull

Pull extracted 2026-09-14T18:52:57.193120+00:00; snapshot 2026-09-14; 80,056 trials with results; parser rules `parsers.R@2026-08-17 / outcomes@2026-09-10 / units@2026-09-14` (module 1.1.0); pipeline commit `3425a4f10c2c3d91ccceccb4d2d805113f104898`.

## A. Reported gender

| Quantity | Old engine | New parser |
|---|---|---|
| Trials reported gender | 1,676 | 665 |
| Old gender-reported that flip to sex-only (reported_sex, not reported_gender) | | 1,405 |
| Old gender-reported that end in another state (not reported_sex) | | 0 |
| Newly reported gender (not gender-reported before) | | 394 |
| gender_labeled_binary_only (Gender-titled, Female/Male or Woman/Man only) | | 2,955 |

Old gender category totals (participants, over old gender-reported trials) vs new buckets (over trials with reported_sex AND is_participant_count):

| Old category | Participants | New bucket | Participants |
|---|---|---|---|
| woman | 39,164 | female (see C) |  |
| man | 23,308 | male (see C) |  |
| nonbinary | 3,498 | gender_diverse | 20,220 |
| transgender | 1,371 | ambiguous | 2,722 |
| other | 589 |  |  |
| unknown | 1,239,450 |  |  |
| of old unknown, inferred by balancing | 128,825 | | |

## B. Sex reporting and the five states

| Quantity | Old engine | New parser |
|---|---|---|
| Trials reported sex | 77,906 | 79,219 |
| of new reported_sex, previously not reported: Gender-titled binary tables | | 1,354 |
| status reported | | 79,270 |
| status explicit_unknown_only | | 66 |
| status uninformative | | 720 |
| status not_reported | | 0 |
| status parse_error | | 0 |

Old sex.reported by new status:

| Old | New status | Trials |
|---|---|---|
| old_not_reported | reported | 1,447 |
| old_not_reported | uninformative | 703 |
| old_reported | explicit_unknown_only | 66 |
| old_reported | reported | 77,823 |
| old_reported | uninformative | 17 |

## C. Sex bucket totals (participants)

| Line | Old engine (sex.reported trials) | New parser (reported_sex AND is_participant_count) |
|---|---|---|
| Trials in the total | 77,906 | 79,107 |
| Female | 55,582,100 | 51,775,993 |
| Male | 48,540,358 | 45,070,116 |
| Explicit Unknown (categories mapped to unknown) | 460,666 | 259,436 |
| Inferred remainder (old: added to Unknown by balancing) | 3,285,606 | 0 (none; see next line) |
| Unknown as displayed by the old tile | 3,746,272 | 259,436 |
| enrollment_minus_parsed, summed over reported rows (stored, shown nowhere as unknown) | | 22,719,864 (positive gaps only: 26,068,818) |
| Gender diverse | | 20,220 |
| Cis/trans-qualified | | 2,722 |

Share of the old Unknown tile that was the inferred remainder: **87.7%** (the tile shrinks by about that at cutover).

## D. Rows removed from composition by is_participant_count

Reported-sex rows the vendored parser flags is_participant_count = False: **112** trials, 804,167 female / 666,274 male units.

All reported-sex rows by the units class of the count-driving measure (diagnostic; the flagged column is what the vendored parser version excludes):

| Units class | Trials | Female units | Male units | Flagged by parser |
|---|---|---|---|---|
| count_of_units | 86 | 802,622 | 665,128 | 86 |
| mean_or_median | 7 | 379 | 269 | 7 |
| percent_like | 19 | 1,165 | 876 | 19 |
| participant_count_like | 79,107 | 51,775,993 | 45,070,116 | 0 |

Industry tab Sex-tier cohort (old rule: interventional, not terminated, PCD >= 2009, attributed industry company, both legacy counts > 0): **27,524** trials.

| Units class | Cohort trials | Female (old totals) | Male (old totals) | Flagged by parser |
|---|---|---|---|---|
| count_of_units | 17 | 1,121 | 1,123 | 17 |
| mean_or_median | 2 | 182,144 | 114,873 | 2 |
| percent_like | 4 | 451 | 311 | 4 |
| participant_count_like | 27,501 | 7,151,042 | 7,242,302 | 0 |

## E. Percent female by results-posted year

| Year | Old (mean f/(f+m+u), unknown incl. inferred) | n | New (a) mean of within-trial f/(f+m) | New (b) participant-weighted | n |
|---|---|---|---|---|---|
| 2009 | 48.4 | 1,067 | 48.7 | 55.7 | 1,096 |
| 2010 | 50.7 | 1,556 | 50.9 | 51.6 | 1,612 |
| 2011 | 50.0 | 2,144 | 50.2 | 54.0 | 2,194 |
| 2012 | 50.7 | 2,713 | 50.8 | 46.5 | 2,787 |
| 2013 | 51.1 | 2,975 | 51.0 | 78.4 | 3,075 |
| 2014 | 48.3 | 4,645 | 48.4 | 55.9 | 4,785 |
| 2015 | 47.8 | 3,663 | 48.0 | 44.8 | 3,773 |
| 2016 | 48.5 | 3,828 | 48.6 | 49.0 | 4,144 |
| 2017 | 49.7 | 5,363 | 49.7 | 51.5 | 5,754 |
| 2018 | 50.3 | 4,609 | 50.5 | 45.0 | 4,600 |
| 2019 | 49.1 | 6,434 | 49.3 | 49.3 | 6,417 |
| 2020 | 50.7 | 5,960 | 50.9 | 56.7 | 5,951 |
| 2021 | 49.2 | 5,740 | 49.4 | 51.9 | 5,726 |
| 2022 | 51.0 | 3,978 | 51.1 | 55.1 | 3,979 |
| 2023 | 52.3 | 5,006 | 52.5 | 55.4 | 4,996 |
| 2024 | 50.8 | 6,730 | 51.0 | 52.0 | 6,715 |
| 2025 | 51.6 | 7,208 | 52.0 | 54.2 | 7,211 |
| 2026 | 51.8 | 4,287 | 52.3 | 54.9 | 4,292 |
