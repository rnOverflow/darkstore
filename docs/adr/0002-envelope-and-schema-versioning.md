# ADR-0002: Event envelope and schema versioning
Status: accepted (P1)
## Decision
Every message is an `Envelope{event_id, event_type, schema_version, occurred_at, order_id, payload}` (JSON). Payloads are pydantic v2 models in `darkstore_common.events`; JSON Schemas are registered in the Redpanda Schema Registry under subject `<topic>-<event_type>`. Rules: within a `schema_version`, changes are additive-only with defaults; a breaking change bumps `schema_version` and producers dual-publish until all consumers upgrade. Trace context travels in Kafka headers, not the envelope.
## Consequences
Consumers ignore unknown fields. Registry compatibility is set to BACKWARD.
