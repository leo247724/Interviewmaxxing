# Application URL inventory

Supabase project: `vhfnrbipsgxfmjrsvdgz`.

This first pass maps each Saved job to its job-specific application URL and application backend bucket. It does not map form fields or submit applications.

`public.application_urls` contains the existing listing/pipeline IDs, original URL, source application URL, backend, status, and a short evidence note. `public.application_backend_counts` groups the results. Run and worker tables keep assignments recoverable. The initial snapshot contains 940 Saved jobs split into ten exclusive batches of 94.

The original job record and local application executor remain in SQLite. This inventory is the Supabase starting point for routing future applications.

```sql
select backend, status, count(*)
from public.application_urls
group by backend, status
order by count(*) desc;
```

Worker outputs use `resolved`, `blocked`, `closed`, or `ambiguous`. An existing direct ATS job URL can be retained. An aggregator URL needs an observed job-specific outbound destination. Easy Apply requires explicit evidence of an in-platform application flow. Unknown destinations remain unresolved.

The migration is managed through Supabase CLI. Worker writes use `scripts/application_urls.py` and psycopg 3. Each worker has a private connection file, restricted to its own rows and mapping columns by PostgreSQL permissions and RLS. Anonymous and general authenticated API access is disabled for these tables.

The task directory `.imx/application-urls/` holds private assignments, runtime handles, connection files, receipts and exports. Never commit its contents or `env.local`. The shared client accepts `list`, `save`, `status`, and `heartbeat`; run `--help` for its arguments.

Database permissions were verified using real worker connections: own rows readable/updatable, other assignments inaccessible, input columns immutable, and anonymous reads denied. Writes used during that check were rolled back.
