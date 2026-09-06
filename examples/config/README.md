# Example configuration

Two files, one per host. Both carry the v1 defaults: the local gate on, the reader on and
pinned to `gpt-5.6-luna`, the optional Suma post-tool mode off, and no writer key at all.

- [`hermes.config.yaml`](hermes.config.yaml) — merge into `~/.hermes/config.yaml`
- [`openclaw.json`](openclaw.json) — merge into your `openclaw.json`

Replace `/path/to/your/project` with the roots you actually want readable. Nothing outside
a configured root can become a source.

Keep each host's adjacent per-plugin LLM policy from the example. It authorizes only the
fixed reader model; it is deliberately outside the plugin-owned `config` object.

Caps in `limits` may only be **narrowed**. A value wider than the contract default in
[`contracts/v1/limits.json`](../../contracts/v1/limits.json) is refused when the plugin
loads, so a config file cannot widen the boundary the acceptance gates measure.

See [`docs/install.md`](../../docs/install.md) for install and uninstall, and
[`docs/capability-matrix.md`](../../docs/capability-matrix.md) for what each host supports.
