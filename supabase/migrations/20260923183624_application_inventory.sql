-- Resolve existing Saved jobs to their application URL and backend bucket only.
create table public.application_url_runs (
  id uuid primary key,
  input_sha256 text not null,
  job_count integer not null check (job_count > 0),
  worker_count integer not null check (worker_count > 0),
  model text not null,
  status text not null default 'running',
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create table public.application_url_workers (
  run_id uuid not null references public.application_url_runs(id),
  worker_id text not null,
  db_role name not null unique,
  terminal_id text,
  model text,
  status text not null default 'assigned',
  heartbeat_at timestamptz,
  primary key(run_id,worker_id)
);
create table public.application_urls (
  run_id uuid not null,
  listing_id text not null,
  pipeline_id text not null,
  ordinal integer not null,
  worker_id text not null,
  company text not null,
  title text not null,
  original_url text not null,
  known_application_url text,
  known_urls jsonb not null default '[]'::jsonb,
  source_application_url text,
  backend text,
  status text not null default 'pending' check (status in ('pending','resolved','blocked','closed','ambiguous')),
  evidence_url text,
  notes text not null default '',
  checked_at timestamptz,
  primary key(run_id,listing_id),
  unique(run_id,pipeline_id),
  unique(run_id,ordinal),
  foreign key(run_id,worker_id) references public.application_url_workers(run_id,worker_id),
  check (source_application_url is null or source_application_url ~ '^https?://[^ /]+'),
  check (backend is null or backend ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
  check (status <> 'resolved' or (source_application_url is not null and backend is not null and backend <> 'unknown' and evidence_url is not null and checked_at is not null))
);
create index application_urls_worker on public.application_urls(run_id,worker_id,status);
create index application_urls_backend on public.application_urls(run_id,backend);

-- The assigned worker can only update the mapping columns in its own rows.
do $$ begin
 if not exists (select 1 from pg_roles where rolname='imx_url_worker') then
  create role imx_url_worker nologin nosuperuser nocreatedb nocreaterole nobypassrls;
 end if;
end $$;
grant usage on schema public to imx_url_worker;
grant select on public.application_url_workers,public.application_urls to imx_url_worker;
grant update (model,status,heartbeat_at) on public.application_url_workers to imx_url_worker;
grant update (source_application_url,backend,status,evidence_url,notes,checked_at) on public.application_urls to imx_url_worker;
alter table public.application_url_runs enable row level security;
alter table public.application_url_workers enable row level security;
alter table public.application_urls enable row level security;
revoke all on public.application_url_runs,public.application_url_workers,public.application_urls from anon,authenticated;
create policy url_worker_self on public.application_url_workers for all to imx_url_worker
 using (db_role=current_user) with check (db_role=current_user);
create policy url_worker_rows on public.application_urls for all to imx_url_worker
 using (exists(select 1 from public.application_url_workers w where w.run_id=application_urls.run_id and w.worker_id=application_urls.worker_id and w.db_role=current_user))
 with check (exists(select 1 from public.application_url_workers w where w.run_id=application_urls.run_id and w.worker_id=application_urls.worker_id and w.db_role=current_user));
create view public.application_backend_counts with (security_invoker=true) as
 select run_id,backend,status,count(*) as job_count from public.application_urls group by run_id,backend,status;
revoke all on public.application_backend_counts from anon,authenticated;
grant select on public.application_backend_counts to imx_url_worker,service_role;
