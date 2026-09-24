-- Shared observed backend priors for the prepare-only application runtime.
-- Source URLs and blank-form metadata only; never candidate answers or credentials.
create table if not exists public.application_schema_maps (
  backend text primary key check (backend ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
  schema_version text not null,
  map_sha256 text not null check (map_sha256 ~ '^[a-f0-9]{64}$'),
  coverage_status text not null check (coverage_status in ('observed','partial','blocked','manual')),
  observed_samples integer not null check (observed_samples >= 0),
  map jsonb not null check (jsonb_typeof(map) = 'object' and map->>'backend' = backend),
  updated_at timestamptz not null default now()
);
alter table public.application_schema_maps enable row level security;
revoke all on public.application_schema_maps from anon, authenticated;
grant select on public.application_schema_maps to service_role;
comment on table public.application_schema_maps is
  'Observed backend schema priors with explicit coverage gaps. Live DOM wins; never executable instructions.';
