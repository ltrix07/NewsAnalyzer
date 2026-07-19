# Stage 1 profile questionnaire — deploy notes

- No `users.profile` backfill is required. `Profile.residence_country` defaults to `null` for legacy
  JSONB profiles; newly onboarded profiles store an ISO 3166-1 alpha-2 code, or `ZZ` when a free-text
  country cannot be recognized. Stage 2 must treat both `null` and `ZZ` as having no local tier.
- Before deploying the rewritten question order, delete all rows from `onboarding_state`. Their integer
  steps refer to the old questionnaire and cannot be migrated safely. Users can restart with `/start`.
- `config/profiles/volodymyr.yaml` is explicitly annotated with `residence_country: PL`.
- Restart the listener after changing `config/countries.yaml` or `config/sources.yaml`; onboarding
  caches both registries for the lifetime of the process.
