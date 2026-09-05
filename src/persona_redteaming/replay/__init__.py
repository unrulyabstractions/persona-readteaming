"""Teacher-forced LCP replay harvester for genlog/v1 rollout logs.

Phase 2 of docs/activation-pipeline.md. Consumes the binding contract
temp/50-genlog-schema.md: the provider writes generations.jsonl, this
package replays it teacher-forced with KV-cache LCP reuse, captures
residual-stream activations on a layer subset, gates every record, and
writes a safetensors store plus a manifest that joins vectors back to
transcript semantics.
"""

__version__ = "0.1.0"
