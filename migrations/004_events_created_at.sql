-- Daily budget check sums today's cost_usd on every /chat turn (S14).
create index if not exists events_created_at_idx on events (created_at);
