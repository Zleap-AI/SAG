export type RuntimeState =
  | { phase: "stopped" | "starting" | "ready" | "stopping" }
  | { phase: "error"; message: string };

export interface RuntimeSession {
  readonly failure: Promise<Error>;
  stop(): Promise<void>;
}

/** Keeps the cleanup handle even when startup never produced a usable session. */
export class RuntimeStartError extends AggregateError {
  readonly cleanup: () => Promise<void>;
  constructor(errors: unknown[], cleanup: () => Promise<void>) {
    super(errors, "本地服务启动失败且未能完成清理，请退出 SAG 后重试");
    this.cleanup = cleanup;
  }
}

/** Owns a single runtime generation, including startup cancellation and teardown. */
export class RuntimeController<T extends RuntimeSession> {
  state: RuntimeState = { phase: "stopped" };
  session: T | undefined;
  private starting: Promise<T> | undefined;
  private stopping: Promise<void> | undefined;
  private abort: AbortController | undefined;
  private cleanupError: unknown;
  private partialCleanup: (() => Promise<void>) | undefined;
  private readonly create: (signal: AbortSignal) => Promise<T>;
  private readonly publish: (state: RuntimeState) => void;

  constructor(create: (signal: AbortSignal) => Promise<T>, publish: (state: RuntimeState) => void = () => {}) {
    this.create = create;
    this.publish = publish;
  }

  private update(state: RuntimeState): void {
    this.state = state;
    this.publish(state);
  }

  start(): Promise<T> {
    if (this.starting) return this.starting;
    if (this.stopping) return this.stopping.then(() => this.start());
    if (this.cleanupError) return Promise.reject(this.cleanupError);
    if (this.session) return Promise.resolve(this.session);
    const abort = new AbortController();
    this.abort = abort;
    this.update({ phase: "starting" });
    this.starting = Promise.resolve().then(() => this.create(abort.signal)).then(async (session) => {
      this.session = session;
      abort.signal.throwIfAborted();
      this.update({ phase: "ready" });
      void session.failure.then(async (error) => {
        if (this.session !== session || this.stopping || abort.signal.aborted) return;
        try { await this.stop(); } catch (cleanupError) {
          error = new AggregateError([error, cleanupError], "服务异常退出且清理失败");
        }
        this.update({ phase: "error", message: error.message });
      });
      return session;
    }).catch((error: unknown) => {
      // The native factory uses AggregateError only when partial-start cleanup
      // also failed. Do not let a retry overlap those potentially live children.
      if (error instanceof AggregateError) this.cleanupError = error;
      if (error instanceof RuntimeStartError) this.partialCleanup = error.cleanup;
      if (!abort.signal.aborted) {
        this.update({ phase: "error", message: error instanceof Error ? error.message : String(error) });
      }
      throw error;
    }).finally(() => { this.starting = undefined; });
    return this.starting;
  }

  stop(): Promise<void> {
    if (this.stopping) return this.stopping;
    this.abort?.abort(new Error("Runtime startup cancelled"));
    this.update({ phase: "stopping" });
    const starting = this.starting;
    this.stopping = (async () => {
      try { await starting; } catch { /* Cleanup is owned below, including partial startup. */ }
      if (this.session) await this.session.stop();
      else if (this.partialCleanup) await this.partialCleanup();
      else if (this.cleanupError) throw this.cleanupError;
      this.session = undefined;
      this.partialCleanup = undefined;
      this.cleanupError = undefined;
      this.update({ phase: "stopped" });
    })().catch((error: unknown) => {
      this.cleanupError = error;
      this.update({ phase: "error", message: error instanceof Error ? error.message : String(error) });
      throw error;
    }).finally(() => { this.stopping = undefined; });
    return this.stopping;
  }
}
