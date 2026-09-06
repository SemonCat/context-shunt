/**
 * Non-content metrics.
 *
 * Labels are bounded enums only. Source paths, questions, answers, quotes, payloads,
 * secrets and full request/source identifiers are never labels - a high-cardinality label
 * is both a cost problem and a leak channel.
 */
/**
 * Closed set of label keys. `model` and `provider` are deliberately absent: a model or
 * provider name is unbounded vendor-controlled text, it changes with configuration, and as a
 * metric dimension it is both a cost problem and a way to fingerprint a deployment. Which
 * model was requested belongs in the envelope's provenance block, where it is bounded and
 * attributed, not in a time series.
 */
export const ALLOWED_LABEL_KEYS = new Set([
  "adapter", "mode", "reason", "status", "code", "form", "decision", "result", "stage",
]);
const LABEL_VALUE = /^[A-Za-z0-9_.:/-]{1,64}$/;

export class MetricsError extends Error {}

export function checkLabels(labels?: Record<string, unknown>): Record<string, string> {
  if (!labels) return {};
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(labels)) {
    if (!ALLOWED_LABEL_KEYS.has(key)) throw new MetricsError(`label key not allowed: ${key}`);
    const text = String(value);
    if (!LABEL_VALUE.test(text)) {
      throw new MetricsError(`label value not a bounded enum token: ${key}`);
    }
    out[key] = text;
  }
  return out;
}

export interface MetricsSink {
  count(name: string, labels?: Record<string, unknown>, value?: number): void;
  observe(name: string, value: number, labels?: Record<string, unknown>): void;
}

export const nullMetrics: MetricsSink = {
  count(_name, labels) {
    checkLabels(labels);
  },
  observe(_name, _value, labels) {
    checkLabels(labels);
  },
};

/** Used by the gates to assert what is (and is not) recorded. */
export class InMemoryMetrics implements MetricsSink {
  readonly entries: Array<{ name: string; value: number; labels: Record<string, string> }> = [];

  count(name: string, labels?: Record<string, unknown>, value = 1): void {
    this.entries.push({ name, value, labels: checkLabels(labels) });
  }

  observe(name: string, value: number, labels?: Record<string, unknown>): void {
    this.entries.push({ name, value, labels: checkLabels(labels) });
  }

  rendered(): string {
    return this.entries.map((e) => `${e.name}${JSON.stringify(e.labels)}=${e.value}`).join("\n");
  }
}
