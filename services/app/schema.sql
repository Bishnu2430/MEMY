-- Application schema. Idempotent; applied at API startup under an advisory lock.
-- LangGraph keeps its own checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes).

CREATE TABLE IF NOT EXISTS orgs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teams (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES orgs(id),
  name text NOT NULL,
  slug text UNIQUE NOT NULL,
  budget_usd_per_task numeric(10,4) NOT NULL DEFAULT 2.0,
  budget_tokens_per_task integer NOT NULL DEFAULT 400000,
  plan_approval_usd numeric(10,4) NOT NULL DEFAULT 1.0,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES orgs(id),
  email text UNIQUE NOT NULL,
  name text NOT NULL,
  password_hash text NOT NULL,
  is_owner boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memberships (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
  PRIMARY KEY (user_id, team_id)
);

CREATE TABLE IF NOT EXISTS agents (
  id text PRIMARY KEY,
  team_id uuid REFERENCES teams(id),          -- NULL = shared across the org
  current_version integer NOT NULL DEFAULT 1,
  enabled boolean NOT NULL DEFAULT true,
  cloned_from text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_versions (
  agent_id text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  version integer NOT NULL,
  spec jsonb NOT NULL,
  yaml text NOT NULL,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, version)
);

CREATE TABLE IF NOT EXISTS agent_cards (
  agent_id text PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
  version integer NOT NULL,
  card_text text NOT NULL,
  embedding vector(384)
);

CREATE TABLE IF NOT EXISTS skills (
  name text PRIMARY KEY,
  description text NOT NULL,
  storage_path text NOT NULL,
  content_hash text NOT NULL,
  approved_hash text,
  enabled boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_servers (
  id text PRIMARY KEY,
  name text NOT NULL,
  transport text NOT NULL CHECK (transport IN ('stdio','http')),
  command text,
  args jsonb NOT NULL DEFAULT '[]',
  url text,
  status text NOT NULL DEFAULT 'active',
  last_error text,
  refreshed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_tools (
  server_id text NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  input_schema jsonb NOT NULL DEFAULT '{}',
  current_hash text NOT NULL,
  pinned_hash text,
  status text NOT NULL DEFAULT 'needs_review',
  side_effect boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (server_id, name)
);

CREATE TABLE IF NOT EXISTS tool_grants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  tool text NOT NULL,                          -- exact name or prefix pattern like mcp.time.*
  needs_approval boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, tool)
);

CREATE TABLE IF NOT EXISTS credentials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider text UNIQUE NOT NULL,
  ciphertext bytea NOT NULL,
  nonce bytea NOT NULL,
  key_version integer NOT NULL DEFAULT 1,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS instructions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type text NOT NULL CHECK (scope_type IN ('org','team','project','agent')),
  scope_id text NOT NULL,
  version integer NOT NULL,
  body text NOT NULL,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_type, scope_id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL REFERENCES teams(id),
  project text,
  created_by uuid NOT NULL REFERENCES users(id),
  title text NOT NULL,
  request text NOT NULL,
  goal text,
  acceptance jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL,
  mode text NOT NULL DEFAULT 'auto',
  requested_agent text,
  router jsonb,
  budget_usd numeric(10,4) NOT NULL,
  budget_tokens integer NOT NULL,
  parent_task_id uuid REFERENCES tasks(id),
  schedule_id uuid,
  private boolean NOT NULL DEFAULT false,
  result text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS tasks_team_idx ON tasks(team_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);

CREATE TABLE IF NOT EXISTS task_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ord integer NOT NULL DEFAULT 0,
  key text NOT NULL,
  title text NOT NULL,
  instruction text NOT NULL,
  agent_id text NOT NULL,
  agent_version integer,
  status text NOT NULL DEFAULT 'pending',
  depends_on text[] NOT NULL DEFAULT '{}',
  attempts integer NOT NULL DEFAULT 0,
  reassignments integer NOT NULL DEFAULT 0,
  agents_tried text[] NOT NULL DEFAULT '{}',
  correction text,
  output text,
  error text,
  verification jsonb,
  review jsonb,
  thread_id text NOT NULL,
  locked_by text,
  lease_until timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  last_event_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_steps_status_idx ON task_steps(status);
CREATE INDEX IF NOT EXISTS task_steps_task_idx ON task_steps(task_id, ord);

CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY,
  task_id uuid,
  step_id uuid,
  team_id uuid,
  type text NOT NULL,
  actor text NOT NULL DEFAULT 'system',
  payload jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_task_idx ON events(task_id, id);
CREATE INDEX IF NOT EXISTS events_team_idx ON events(team_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid REFERENCES tasks(id) ON DELETE CASCADE,
  step_id uuid,
  team_id uuid NOT NULL,
  name text NOT NULL,
  kind text NOT NULL,
  mime text NOT NULL,
  storage_path text NOT NULL,
  content_hash text NOT NULL,
  size integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL REFERENCES teams(id),
  project text,
  kind text NOT NULL,                          -- upload | url | memory | task_summary | text
  url text,
  domain text,
  title text NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  content_hash text NOT NULL,
  simhash bigint,
  scores jsonb NOT NULL DEFAULT '{}',
  trust_score real NOT NULL DEFAULT 0.5,
  status text NOT NULL DEFAULT 'live',         -- live | quarantined | superseded
  excerpt text,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sources_team_idx ON sources(team_id, status);

CREATE TABLE IF NOT EXISTS chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  team_id uuid NOT NULL,
  project text,
  ord integer NOT NULL,
  content text NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding vector(384),
  embedding_model text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin(tsv);
DO $$
BEGIN
  -- Embedding model changed (768-d -> 384-d): keep rows, clear vectors; cards re-embed automatically.
  IF EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
             WHERE c.relname = 'chunks' AND a.attname = 'embedding'
               AND format_type(a.atttypid, a.atttypmod) <> 'vector(384)') THEN
    DROP INDEX IF EXISTS chunks_emb_idx;
    ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(384) USING NULL;
    ALTER TABLE agent_cards ALTER COLUMN embedding TYPE vector(384) USING NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS chunks_emb_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS model_limits (
  alias text PRIMARY KEY,
  limit_tokens integer,
  remaining_tokens integer,
  blocked_until timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_team_idx ON chunks(team_id);

CREATE TABLE IF NOT EXISTS memory_proposals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL,
  agent_id text,
  task_id uuid,
  fact text NOT NULL,
  source_url text,
  status text NOT NULL DEFAULT 'pending',
  reviewed_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS schedules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL REFERENCES teams(id),
  created_by uuid NOT NULL REFERENCES users(id),
  name text NOT NULL,
  cron text NOT NULL,
  request text NOT NULL,
  agent_id text,
  next_run_at timestamptz NOT NULL,
  last_run_at timestamptz,
  last_task_id uuid,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  step_id uuid NOT NULL,
  team_id uuid NOT NULL,
  kind text NOT NULL DEFAULT 'tool',           -- tool | plan
  tool text,
  args jsonb NOT NULL DEFAULT '{}',
  tool_call_id text NOT NULL,
  reason text,
  status text NOT NULL DEFAULT 'pending',
  requested_at timestamptz NOT NULL DEFAULT now(),
  decided_by uuid,
  decided_at timestamptz,
  UNIQUE (step_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS usage_records (
  id bigserial PRIMARY KEY,
  user_id uuid,
  team_id uuid,
  agent_id text,
  task_id uuid,
  step_id uuid,
  kind text NOT NULL,                          -- llm | embed | tool
  model text,
  provider text,
  tokens_in integer NOT NULL DEFAULT 0,
  tokens_out integer NOT NULL DEFAULT 0,
  cost_usd numeric(12,6) NOT NULL DEFAULT 0,
  latency_ms integer NOT NULL DEFAULT 0,
  cache_hit boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'ok',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_task_idx ON usage_records(task_id);
CREATE INDEX IF NOT EXISTS usage_time_idx ON usage_records(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY,
  actor uuid,
  action text NOT NULL,
  target text,
  detail jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS controls (
  scope text PRIMARY KEY,                      -- all | team:<uuid> | agent:<id>
  paused boolean NOT NULL DEFAULT false,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS routing_log (
  id bigserial PRIMARY KEY,
  task_id uuid,
  team_id uuid,
  request text NOT NULL,
  shortlist jsonb NOT NULL DEFAULT '[]',
  chosen text,
  mode text,
  confidence real,
  reason text,
  overridden_to text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Row-level security: knowledge is readable only inside the caller's team scope.
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'chunks' AND policyname = 'chunks_team_scope') THEN
    CREATE POLICY chunks_team_scope ON chunks
      USING (team_id = ANY (string_to_array(current_setting('app.team_ids', true), ',')::uuid[]));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'sources' AND policyname = 'sources_team_scope') THEN
    CREATE POLICY sources_team_scope ON sources
      USING (team_id = ANY (string_to_array(current_setting('app.team_ids', true), ',')::uuid[]));
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO app_scoped;
GRANT SELECT ON sources, chunks TO app_scoped;

-- v0.1.1: media, attachments, knowledge provenance, MCP secrets
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}';
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS created_by uuid;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'output';   -- input | output
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS extracted text;                         -- text pulled from an input file
CREATE INDEX IF NOT EXISTS artifacts_team_idx ON artifacts(team_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_task_idx ON artifacts(task_id);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';

ALTER TABLE sources ADD COLUMN IF NOT EXISTS task_id uuid;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS step_id uuid;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS agent_id text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS artifact_id uuid;
CREATE INDEX IF NOT EXISTS sources_task_idx ON sources(task_id);

CREATE TABLE IF NOT EXISTS media_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id uuid NOT NULL REFERENCES teams(id),
  user_id uuid NOT NULL REFERENCES users(id),
  kind text NOT NULL,                          -- speech | sound | music | image | video | diagram | transcribe
  params jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'queued',       -- queued | running | completed | failed
  progress text,
  artifact_id uuid,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS secret_env jsonb NOT NULL DEFAULT '{}';   -- ENV_VAR -> credential provider
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS headers jsonb NOT NULL DEFAULT '{}';      -- header -> credential provider
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS context_env boolean NOT NULL DEFAULT false; -- pass team/project to server
ALTER TABLE agents ADD COLUMN IF NOT EXISTS seed_hash text;
