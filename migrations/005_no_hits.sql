-- Answer whose knowledge search found nothing above the similarity floor (S18).
alter table events add column if not exists no_hits boolean;
