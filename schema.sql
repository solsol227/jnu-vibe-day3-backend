-- Supabase -> SQL Editor 에 붙여 넣고 Run
create table if not exists jnu_vc3_air_readings (
  id           bigint generated always as identity primary key,
  source       text not null,                 -- 'airkorea'(진짜) / 'demo'(연습)
  sido         text not null,
  station      text not null,                 -- 측정소 이름
  data_time    timestamptz not null,          -- 측정 시각
  pm10         int,
  pm25         int,
  o3           real,
  no2          real,
  pm10_grade   text,
  pm25_grade   text,
  collected_at timestamptz not null default now(),
  unique (source, station, data_time)
);

create table if not exists jnu_vc3_collect_logs (
  id           bigint generated always as identity primary key,
  ran_at       timestamptz not null default now(),
  source       text,
  fetched      int,
  fixed_values int,
  dropped      int,
  cleaned      int,
  saved        int
);

-- 브라우저에서 직접 못 건드리게 잠금. FastAPI는 secret 키라서 통과.
alter table jnu_vc3_air_readings enable row level security;
alter table jnu_vc3_collect_logs enable row level security;
