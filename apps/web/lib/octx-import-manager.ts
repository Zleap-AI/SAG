import {
  isTerminalImportStatus,
  parseImportTasks,
  serializeImportTasks,
  type PersistedOctxImportTask,
} from "@/lib/octx-import-tasks";
import type { OctxImportAction, OctxTransfer } from "@/lib/types";

interface Dependencies {
  load: () => string | null;
  save: (value: string) => void;
  startImport: (file: File, transferId: string) => Promise<OctxTransfer>;
  getTransfer: (id: string) => Promise<OctxTransfer>;
  cancelTransfer: (id: string) => Promise<OctxTransfer>;
  decideImport: (
    id: string,
    body: {
      action: OctxImportAction;
      decision_token: string;
      target_source_id?: string;
      discard_local_changes?: boolean;
    },
  ) => Promise<OctxTransfer>;
  newTransferId: () => string;
  now: () => string;
}

type Listener = (tasks: PersistedOctxImportTask[]) => void;

export class OctxImportTaskManager {
  private tasks: PersistedOctxImportTask[] = [];
  private listeners = new Set<Listener>();

  constructor(private dependencies: Dependencies) {}

  snapshot() { return this.tasks; }
  subscribe(listener: Listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  hydrate() {
    this.tasks = parseImportTasks(this.dependencies.load());
    this.emit();
  }
  async start(file: File) {
    const transferId = this.dependencies.newTransferId();
    const createdAt = this.dependencies.now();
    const pending = {
      transferId,
      filename: file.name,
      createdAt,
      transfer: {
        id: transferId,
        direction: "import" as const,
        status: "uploaded" as const,
        progress: 0,
        asset: null,
        release: null,
        target_source_id: null,
        installation_id: null,
        allowed_actions: [],
        decision_token: null,
        conflicts: [],
        excluded_documents: [],
        record_counts: {},
        capabilities: {},
        progress_detail: { phase: "uploaded" },
        validation_report: null,
        warnings: [],
        error: null,
        cancellation_requested: false,
        created_at: createdAt,
        updated_at: createdAt,
      },
    };
    this.reconcile(pending, pending.transfer);
    const transfer = await this.dependencies.startImport(file, transferId);
    return this.reconcile(pending, transfer);
  }
  async refresh() {
    await Promise.all(this.tasks.filter((task) => !isTerminalImportStatus(task.transfer.status))
      .map(async (task) => {
        try { this.reconcile(task, await this.dependencies.getTransfer(task.transferId)); } catch { /* transient */ }
      }));
  }
  async cancel(id: string) {
    const task = this.tasks.find((item) => item.transferId === id);
    if (task) this.reconcile(task, await this.dependencies.cancelTransfer(id));
  }
  async decide(id: string, body: Parameters<Dependencies["decideImport"]>[1]) {
    const task = this.tasks.find((item) => item.transferId === id);
    if (task) this.reconcile(task, await this.dependencies.decideImport(id, body));
  }
  dismiss(id: string) {
    this.tasks = this.tasks.filter((task) => task.transferId !== id);
    this.persist();
  }
  acknowledgeCompletion(id: string) {
    this.tasks = this.tasks.map((task) => task.transferId === id
      ? { ...task, completionNotified: true }
      : task);
    this.persist();
  }
  private reconcile(task: PersistedOctxImportTask, transfer: OctxTransfer) {
    const next = { ...task, transferId: transfer.id, transfer };
    this.tasks = [next, ...this.tasks.filter((item) => item.transferId !== transfer.id)];
    this.persist();
    return next;
  }
  private persist() {
    this.dependencies.save(serializeImportTasks(this.tasks));
    this.emit();
  }
  private emit() { for (const listener of this.listeners) listener(this.tasks); }
}
