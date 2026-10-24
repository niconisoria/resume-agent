-- One feedback row per assistant message (latest vote wins via upsert, S13).
create unique index if not exists events_feedback_message_idx
    on events (message_id) where type = 'feedback';
