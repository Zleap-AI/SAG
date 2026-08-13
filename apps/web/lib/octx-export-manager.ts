import {
  isTerminalExportStatus,
  mergeExportTransfer,
  parseExportTasks,
  serializeExportTasks,
  type NewOctxExportTask,
  type PersistedOctxExportTask,
} from "@/lib/octx-export-tasks";
import type { OctxTransfer } from "@/lib/types";

export interface OctxExportManagerDependencies {
  load: () => string | null;
  save: (value: string) => void;
  getTransfer: (transferId: string) => Promise<OctxTransfer>;
  startExport: (sourceId: string) => Promise<OctxTransfer>;
  decideExport: (transferId: string, decisionToken: string) => Promise<OctxTransfer>;
  cancelDecision: (transferId: string, decisionToken: string) => Promise<OctxTransfer>;
  cancelTransfer: (transferId: string) => Promise<OctxTransfer>;
  download: (task: PersistedOctxExportTask) => Promise<void>;
  now: () => string;
}

type Listener = (tasks: PersistedOctxExportTask[]) => void;

export class OctxExportTaskManager {
  private tasks: PersistedOctxExportTask[] = [];
  private readonly listeners = new Set<Listener>();
  private readonly autoDownloadAttempts = new Set<string>();

  constructor(private readonly dependencies: OctxExportManagerDependencies) {}

  snapshot(): PersistedOctxExportTask[] {
    return this.tasks;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async hydrate(): Promise<void> {
    this.tasks = parseExportTasks(this.dependencies.load());
    this.emit();
    for (const task of this.tasks) {
      if (task.transfer.status === "ready" && !task.autoDownloaded) {
        await this.autoDownload(task);
      }
    }
  }

  async start(
    sourceId: string,
    sourceName: string,
    filenameHint?: string,
  ): Promise<PersistedOctxExportTask> {
    const transfer = await this.dependencies.startExport(sourceId);
    const metadata: NewOctxExportTask = {
      transferId: transfer.id,
      sourceId,
      sourceName,
      filenameHint,
      createdAt: this.dependencies.now(),
    };
    return this.reconcile(metadata, transfer);
  }

  async refresh(): Promise<void> {
    const active = this.tasks.filter(
      (task) => !isTerminalExportStatus(task.transfer.status),
    );
    await Promise.all(
      active.map(async (task) => {
        try {
          const transfer = await this.dependencies.getTransfer(task.transferId);
          await this.reconcile(task, transfer);
        } catch {
          // A transient browser/network failure must not overwrite server state.
        }
      }),
    );
  }

  async confirmReadyOnly(
    transferId: string,
    decisionToken: string,
  ): Promise<PersistedOctxExportTask | null> {
    const task = this.tasks.find((item) => item.transferId === transferId);
    if (!task) return null;
    const transfer = await this.dependencies.decideExport(transferId, decisionToken);
    return this.reconcile(task, transfer);
  }

  async cancel(transferId: string): Promise<PersistedOctxExportTask | null> {
    const task = this.tasks.find((item) => item.transferId === transferId);
    if (!task) return null;
    const transfer = await this.dependencies.cancelTransfer(transferId);
    return this.reconcile(task, transfer);
  }

  async cancelDecision(
    transferId: string,
    decisionToken: string,
  ): Promise<PersistedOctxExportTask | null> {
    const task = this.tasks.find((item) => item.transferId === transferId);
    if (!task) return null;
    const transfer = await this.dependencies.cancelDecision(transferId, decisionToken);
    return this.reconcile(task, transfer);
  }

  async downloadAgain(transferId: string): Promise<void> {
    const task = this.tasks.find((item) => item.transferId === transferId);
    if (!task || task.transfer.status !== "ready") return;
    await this.dependencies.download(task);
    this.markDownloaded(transferId);
  }

  dismiss(transferId: string): void {
    const remaining = this.tasks.filter((task) => task.transferId !== transferId);
    if (remaining.length === this.tasks.length) return;
    this.tasks = remaining;
    this.autoDownloadAttempts.delete(transferId);
    this.persistAndEmit();
  }

  private async reconcile(
    metadata: NewOctxExportTask,
    transfer: OctxTransfer,
  ): Promise<PersistedOctxExportTask> {
    this.tasks = mergeExportTransfer(this.tasks, metadata, transfer);
    this.persistAndEmit();
    const task = this.tasks.find((item) => item.transferId === transfer.id)!;
    if (transfer.status === "ready" && !task.autoDownloaded) {
      await this.autoDownload(task);
    }
    return this.tasks.find((item) => item.transferId === transfer.id)!;
  }

  private async autoDownload(task: PersistedOctxExportTask): Promise<void> {
    if (this.autoDownloadAttempts.has(task.transferId)) return;
    this.autoDownloadAttempts.add(task.transferId);
    try {
      await this.dependencies.download(task);
      this.markDownloaded(task.transferId);
    } catch {
      // Keep autoDownloaded=false so the visible task offers a manual retry.
    }
  }

  private markDownloaded(transferId: string): void {
    this.tasks = this.tasks.map((task) =>
      task.transferId === transferId ? { ...task, autoDownloaded: true } : task,
    );
    this.persistAndEmit();
  }

  private persistAndEmit(): void {
    this.dependencies.save(serializeExportTasks(this.tasks));
    this.emit();
  }

  private emit(): void {
    const snapshot = this.snapshot();
    for (const listener of this.listeners) listener(snapshot);
  }
}
