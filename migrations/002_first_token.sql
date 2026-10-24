-- Time to first visible text, per answer (S12).
alter table events add column if not exists first_token_ms int;
