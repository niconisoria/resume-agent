-- Idempotent: safe to run on every ingest.
create extension if not exists vector;

create table if not exists documents (
    id bigserial primary key,
    path text not null unique,
    title text not null,
    metadata jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

create table if not exists chunks (
    id bigserial primary key,
    document_id bigint not null references documents (id) on delete cascade,
    content text not null,
    embedding vector(1024) not null,
    position int not null
);

create index if not exists chunks_embedding_idx
    on chunks using hnsw (embedding vector_cosine_ops);

create table if not exists conversations (
    id uuid primary key,
    ip_hash text not null,
    status text not null default 'open',
    turns int not null default 0,
    created_at timestamptz not null default now()
);

create table if not exists messages (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations (id) on delete cascade,
    role text not null check (role in ('user', 'assistant')),
    content text not null,
    created_at timestamptz not null default now()
);

create table if not exists events (
    id bigserial primary key,
    conversation_id uuid references conversations (id) on delete set null,
    message_id uuid references messages (id) on delete set null,
    type text not null check (type in (
        'answer', 'tool_call', 'escalation', 'feedback', 'rate_limited', 'budget_exceeded'
    )),
    topic text,
    tool_name text,
    latency_ms int,
    input_tokens int,
    output_tokens int,
    cost_usd numeric(10, 6),
    rating smallint check (rating in (-1, 1)),
    created_at timestamptz not null default now()
);
