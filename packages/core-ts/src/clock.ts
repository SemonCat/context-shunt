/** Deadlines and cancellation with an injectable clock. */
import { ShuntError } from "./errors.js";

export interface Clock {
  nowMs(): number;
}

export const monotonicClock: Clock = {
  nowMs: () => Math.round(Number(process.hrtime.bigint() / 1000n) / 1000),
};

export class FakeClock implements Clock {
  private current = 0;
  nowMs(): number {
    return this.current;
  }
  advance(ms: number): void {
    this.current += ms;
  }
}

export class Deadline {
  private readonly controller = new AbortController();

  private constructor(
    private readonly clock: Clock,
    private readonly startedMs: number,
    private readonly budgetMs: number,
    externalSignal?: AbortSignal,
  ) {
    if (externalSignal?.aborted) {
      this.controller.abort();
    } else if (externalSignal) {
      externalSignal.addEventListener("abort", () => this.controller.abort(), { once: true });
    }
  }

  static start(clock: Clock, budgetMs: number, signal?: AbortSignal): Deadline {
    return new Deadline(clock, clock.nowMs(), budgetMs, signal);
  }

  elapsedMs(): number {
    return this.clock.nowMs() - this.startedMs;
  }

  remainingMs(): number {
    return Math.max(0, this.budgetMs - this.elapsedMs());
  }

  expired(): boolean {
    return this.remainingMs() <= 0;
  }

  cancel(): void {
    this.controller.abort();
  }

  isCancelled(): boolean {
    return this.controller.signal.aborted;
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  /** Called before starting new work and before publishing anything. */
  check(stage: string): void {
    if (this.isCancelled()) throw new ShuntError("CANCELLED", stage, false);
    if (this.expired()) throw new ShuntError("TIMEOUT", stage, true);
  }

  /** A stage may never outlive the request budget. */
  subBudget(stageBudgetMs: number): number {
    return Math.min(stageBudgetMs, this.remainingMs());
  }
}
