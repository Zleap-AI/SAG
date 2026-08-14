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
  startDocumentExport?: (sourceId: string, documentId: string) => Promise<OctxTransfer>;
  decideExport: (transferId: string, decisionToken: string) => Promise<OctxTransfer>;
  cancelDecision: (transferId: string, decisionToken: string) => Promise<OctxTransfer>;
  cancelTransfer: (transferId: string) => Promise<OctxTransfer>;
  download: (task: PersistedOctxExportTask) => Promise<void>;
  recordEvent?: (type: "octx.export", data: Record<string, unknown>) => void;
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
    this.dependencies.recordEvent?.("octx.export", {
      action: "created",
      transfer_id: transfer.id,
      source_id: sourceId,
      scope: "source",
      status: transfer.status,
    });
    const metadata: NewOctxExportTask = {
      transferId: transfer.id,
      sourceId,
      sourceName,
      filenameHint,
      createdAt: this.dependencies.now(),
    };
    return this.reconcile(metadata, transfer);
  }

  async startDocument(
    sourceId: string,
    sourceName: string,
    documentId: string,
    documentName: string,
  ): Promise<PersistedOctxExportTask> {
    if (!this.dependencies.startDocumentExport) {
      throw new Error("Document OCTX export is unavailable");
    }
    const transfer = await this.dependencies.startDocumentExport(sourceId, documentId);
    this.dependencies.recordEvent?.("octx.export", {
      action: "created",
      transfer_id: transfer.id,
      source_id: sourceId,
      document_id: documentId,
      scope: "document",
      status: transfer.status,
    });
    return this.reconcile(
      {
        transferId: transfer.id,
        sourceId,
        sourceName,
        filenameHint: documentName,
        createdAt: this.dependencies.now(),
      },
      transfer,
    );
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
    try {
      await this.dependencies.download(task);
      this.markDownloaded(transferId);
    } catch (error) {
      this.markDownloadFailed(task, error);
      throw error;
    }
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
    } catch (error) {
      this.markDownloadFailed(task, error);
    }
  }

  private markDownloadFailed(task: PersistedOctxExportTask, error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    this.tasks = this.tasks.map((item) =>
      item.transferId === task.transferId
        ? { ...item, autoDownloaded: false, downloadError: message.slice(0, 1000) }
        : item,
    );
    this.dependencies.recordEvent?.("octx.export", {
      action: "download_failed",
      transfer_id: task.transferId,
      source_id: task.sourceId,
      scope: task.transfer.export_scope ?? "source",
      error_type: error instanceof Error ? error.name : typeof error,
      error_message: message,
    });
    this.persistAndEmit();
  }

  private markDownloaded(transferId: string): void {
    this.tasks = this.tasks.map((task) =>
      task.transferId === transferId
        ? { ...task, autoDownloaded: true, downloadError: undefined }
        : task,
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
