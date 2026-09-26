export type ExitReason = "quit" | "update";

/** Serializes exit decisions so a quit confirmation cannot authorize an update. */
export class ExitController {
  private pending = false;
  private readonly inspect: () => Promise<boolean>;
  private readonly confirm: (reason: ExitReason, active: boolean | null) => Promise<boolean>;
  private readonly stop: () => Promise<void>;

  constructor(
    inspect: () => Promise<boolean>,
    confirm: (reason: ExitReason, active: boolean | null) => Promise<boolean>,
    stop: () => Promise<void>,
  ) {
    this.inspect = inspect;
    this.confirm = confirm;
    this.stop = stop;
  }

  async prepare(reason: ExitReason): Promise<boolean> {
    if (this.pending) return false;
    this.pending = true;
    try {
      const active = await this.inspect().catch(() => null);
      if (active !== false && !await this.confirm(reason, active)) return false;
      await this.stop();
      return true;
    } finally { this.pending = false; }
  }
}
