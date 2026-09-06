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
  private cancelled = false;

  private constructor(
    private readonly clock: Clock,
    private readonly startedMs: number,
    private readonly budgetMs: number,
  ) {}

  static start(clock: Clock, budgetMs: number): Deadline {
    return new Deadline(clock, clock.nowMs(), budgetMs);
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
    this.cancelled = true;
  }

  isCancelled(): boolean {
    return this.cancelled;
  }

  /** Called before starting new work and before publishing anything. */
  check(stage: string): void {
    if (this.cancelled) throw new ShuntError("CANCELLED", stage, false);
    if (this.expired()) throw new ShuntError("TIMEOUT", stage, true);
  }

  /** A stage may never outlive the request budget. */
  subBudget(stageBudgetMs: number): number {
    return Math.min(stageBudgetMs, this.remainingMs());
  }
}
