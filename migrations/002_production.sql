-- Domain model. Names follow the blueprint's schema; additions are marked (+).

CREATE TABLE productions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, title text NOT NULL, timezone text NOT NULL DEFAULT 'America/Los_Angeles', start_date date NOT NULL, budget numeric NOT NULL,
  shoot_days int NOT NULL, breakdown_status text NOT NULL DEFAULT 'draft' CHECK (breakdown_status IN ('draft','approved')), parser jsonb NOT NULL DEFAULT '{}'::jsonb);          -- (+)
SELECT enable_tenant_rls('productions');

CREATE TABLE locations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, name text NOT NULL, int_ext text NOT NULL, fee numeric NOT NULL);   -- (+)
SELECT enable_tenant_rls('locations');

CREATE TABLE scenes (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, scene_no text NOT NULL, int_ext text NOT NULL, day_night text NOT NULL, pages numeric NOT NULL,
  location_id uuid NOT NULL REFERENCES locations, requirements jsonb NOT NULL DEFAULT '{}'::jsonb, ordinal int NOT NULL);
CREATE INDEX scenes_production ON scenes (production_id, ordinal);
SELECT enable_tenant_rls('scenes');

CREATE TABLE "cast" (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, name text NOT NULL, constraints jsonb NOT NULL DEFAULT '{"unavailable": []}'::jsonb,
  rate jsonb NOT NULL, UNIQUE (production_id, name));
SELECT enable_tenant_rls('cast');

CREATE TABLE scene_cast (scene_id uuid REFERENCES scenes ON DELETE CASCADE, cast_id uuid REFERENCES "cast" ON DELETE CASCADE, PRIMARY KEY (scene_id, cast_id), tenant_id uuid NOT NULL REFERENCES tenants);
CREATE INDEX scene_cast_cast ON scene_cast (cast_id);
SELECT enable_tenant_rls('scene_cast');

-- (+) A scenario is one what-if: its own availability and weather, and the schedule and costs solved for it.
CREATE TABLE scenarios (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, name text NOT NULL, based_on uuid REFERENCES scenarios, config jsonb NOT NULL,
  status text NOT NULL DEFAULT 'draft', result jsonb, created_at timestamptz NOT NULL DEFAULT now());
SELECT enable_tenant_rls('scenarios');

CREATE TABLE shoot_days (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, shoot_date date NOT NULL, call_time time, status text NOT NULL, location_id uuid REFERENCES locations,
  scenario_id uuid NOT NULL REFERENCES scenarios ON DELETE CASCADE, day_index int NOT NULL, UNIQUE (scenario_id, shoot_date));                                                    -- (+)
SELECT enable_tenant_rls('shoot_days');

CREATE TABLE schedule_items (shoot_day_id uuid REFERENCES shoot_days ON DELETE CASCADE, scene_id uuid REFERENCES scenes ON DELETE CASCADE, sequence int NOT NULL, estimated_minutes int NOT NULL, PRIMARY KEY (shoot_day_id, scene_id),
  tenant_id uuid NOT NULL REFERENCES tenants);
CREATE INDEX schedule_items_scene ON schedule_items (scene_id);
SELECT enable_tenant_rls('schedule_items');

CREATE TABLE cost_items (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, production_id uuid NOT NULL REFERENCES productions ON DELETE CASCADE, category text NOT NULL, amount numeric NOT NULL,
  scenario_id uuid NOT NULL REFERENCES scenarios ON DELETE CASCADE);
CREATE INDEX cost_items_scenario ON cost_items (scenario_id);
SELECT enable_tenant_rls('cost_items');
