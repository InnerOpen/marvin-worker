# Backlog

## A message broker (Redis, RabbitMQ) in front of the queue

**Today:** the queue is `job` records in Marvin; one dispatcher polls them (default every 10 s), claims by writing `status: claimed`, and
starts a worker. That is right for tens of jobs a day and keeps everything visible and auditable in Marvin. It will strain when:

- **more than one dispatcher** is wanted (high availability or throughput): the claim is a write, not an atomic take, so two can race;
- **jobs arrive in bursts** (hundreds a minute): polling and one write per state change load Marvin's API;
- **latency matters**: a job waits up to a poll interval before it starts;
- **retries with backoff, priorities, rate limits per kind, or fan-out** are needed: those are queue features, not record features.

**Shape, when it is time:** keep Marvin's `job` record as the source of truth (status, args, result, audit). The broker only carries a
*wake-up* with the record's slug, never the data, so losing the broker loses nothing: on start the dispatcher re-enqueues whatever Marvin
still has as `queued`. Competing consumers (a Redis stream with a consumer group, or a RabbitMQ queue with acks) replace the claim write
and allow any number of dispatchers; a dead consumer's message is redelivered. The dispatcher's `Queue` is the seam: `claim()` becomes a
blocking read from the broker, and the launchers do not change. Who publishes: the dispatcher's poll at first (so Marvin needs no broker
access), or a workflow's `request` step against the broker's HTTP API later.

**Not before:** a measured problem. Revisit when jobs routinely queue behind each other, a second dispatcher is wanted, or the poll shows
up in Marvin's request load.

## Smaller
- A per-job token (minted for one run and expiring) instead of one long-lived pinned token in every worker.
- Kubernetes: read the Job's outcome as well, so a pod killed by the cluster (OOM) fails the record at once, not at the timeout.
- Retries as a field on the `job-kind` (max attempts), with the attempt count already on the record.
