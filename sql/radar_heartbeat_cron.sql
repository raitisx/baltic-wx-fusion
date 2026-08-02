-- Keep the hour when GitHub will not.  (applied 2026-08-02)
--
-- tick.yml is declared hourly. Over its last 21 scheduled fires GitHub ran it
-- at a median interval of 117 minutes, with a worst gap of 4 h 26 m and only
-- three intervals anywhere near sixty. Every one of those runs SUCCEEDED — the
-- schedule simply was not honoured. The radar composite is the freshest thing
-- on the page, so that is what goes visibly stale.
--
-- The clock therefore lives here rather than at GitHub. Every fifteen minutes
-- this calls the wx-actions heartbeat, which reads the radar manifest, works
-- out how old the newest frame is, and starts a tick only if one is overdue
-- (>65 min) and none is already queued or running. On an hour where GitHub
-- does fire, this costs one HTTP call and dispatches nothing.
--
-- The endpoint is deliberately unauthenticated: the only thing it can do is
-- start the job that is meant to be running anyway, and only when the data is
-- already stale. See the note above heartbeat() in wx-actions/index.ts.
--
-- Idempotent — safe to re-run.
create extension if not exists pg_net with schema extensions;
create extension if not exists pg_cron;

select cron.unschedule('wx-radar-heartbeat')
where exists (select 1 from cron.job where jobname = 'wx-radar-heartbeat');

select cron.schedule(
  'wx-radar-heartbeat',
  '5,20,35,50 * * * *',
  $job$
    select net.http_get(
      url := 'https://xxxywjxmdpildafzbnpj.supabase.co/functions/v1/wx-actions?op=heartbeat',
      timeout_milliseconds := 25000)
  $job$);

-- Checking on it:
--   select * from cron.job;
--   select jobid, status, return_message, start_time
--     from cron.job_run_details order by start_time desc limit 10;
--   select id, status_code, content, created
--     from net._http_response order by created desc limit 10;
--
-- Turning it off:
--   select cron.unschedule('wx-radar-heartbeat');
