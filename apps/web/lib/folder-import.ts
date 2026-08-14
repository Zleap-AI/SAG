export type FolderImportDecision = "undecided" | "skip" | "upload";
export type FolderImportStatus = "eligible" | "conflict" | "rejected";
export type FolderImportRejectReason = "unsupported_type" | "file_too_large" | "missing_name";

export interface FolderImportItem {
  id: string;
  file: File;
  name: string;
  displayPath: string;
  status: FolderImportStatus;
  selected: boolean;
  decision: FolderImportDecision;
  rejectReason?: FolderImportRejectReason;
  duplicateWith: "existing" | "folder" | "both" | null;
}

export interface FolderImportPlan {
  items: FolderImportItem[];
  summary: { eligible: number; conflicts: number; rejected: number };
}

function normalizedName(name: string): string {
  return name.normalize("NFC").toLocaleLowerCase();
}

function extensionOf(name: string): string {
  const extensionIndex = name.lastIndexOf(".");
  return extensionIndex === -1 ? "" : name.slice(extensionIndex).toLocaleLowerCase();
}

export function buildFolderImportPlan(
  files: File[],
  existingNames: string[],
  allowedExts: string[],
  maxBytes: number,
): FolderImportPlan {
  const existing = new Set(existingNames.map(normalizedName));
  const allowed = new Set(allowedExts.map((extension) => extension.toLocaleLowerCase()));
  const names = new Map<string, number>();

  for (const file of files) {
    if (file.name) {
      const normalized = normalizedName(file.name);
      names.set(normalized, (names.get(normalized) ?? 0) + 1);
    }
  }

  const items = files.map((file, index): FolderImportItem => {
    const name = file.name;
    const displayPath = file.webkitRelativePath || name;
    const normalized = name ? normalizedName(name) : "";
    let rejectReason: FolderImportRejectReason | undefined;

    if (!name) {
      rejectReason = "missing_name";
    } else if (!allowed.has(extensionOf(name))) {
      rejectReason = "unsupported_type";
    } else if (file.size > maxBytes) {
      rejectReason = "file_too_large";
    }

    if (rejectReason) {
      return {
        id: `folder-import-${index}`,
        file,
        name,
        displayPath,
        status: "rejected",
        selected: false,
        decision: "skip",
        rejectReason,
        duplicateWith: null,
      };
    }

    const duplicateExisting = existing.has(normalized);
    const duplicateFolder = (names.get(normalized) ?? 0) > 1;
    const duplicateWith = duplicateExisting && duplicateFolder
      ? "both"
      : duplicateExisting
        ? "existing"
        : duplicateFolder
          ? "folder"
          : null;

    return {
      id: `folder-import-${index}`,
      file,
      name,
      displayPath,
      status: duplicateWith ? "conflict" : "eligible",
      selected: true,
      decision: duplicateWith ? "undecided" : "upload",
      duplicateWith,
    };
  });

  return {
    items,
    summary: {
      eligible: items.filter((item) => item.status === "eligible").length,
      conflicts: items.filter((item) => item.status === "conflict").length,
      rejected: items.filter((item) => item.status === "rejected").length,
    },
  };
}

export function setFolderImportItemSelected(
  plan: FolderImportPlan,
  itemId: string,
  selected: boolean,
): FolderImportPlan {
  const item = plan.items.find((candidate) => candidate.id === itemId);
  if (!item || item.status === "rejected" || item.selected === selected) return plan;

  return {
    ...plan,
    items: plan.items.map((candidate) =>
      candidate.id === itemId ? { ...candidate, selected } : candidate,
    ),
  };
}

export function setAllFolderImportItemsSelected(
  plan: FolderImportPlan,
  selected: boolean,
): FolderImportPlan {
  return {
    ...plan,
    items: plan.items.map((item) =>
      item.status === "rejected" || item.selected === selected
        ? item
        : { ...item, selected },
    ),
  };
}

export function selectedFolderImportItems(plan: FolderImportPlan): FolderImportItem[] {
  return plan.items.filter((item) => item.selected);
}

export function resolveFolderImportItem(
  plan: FolderImportPlan,
  itemId: string,
  decision: FolderImportDecision,
): FolderImportPlan {
  const item = plan.items.find((candidate) => candidate.id === itemId);
  if (!item) return plan;

  return {
    ...plan,
    items: plan.items.map((candidate) => candidate.id === itemId ? { ...candidate, decision } : candidate),
  };
}

export function hasUnresolvedFolderImportConflicts(plan: FolderImportPlan): boolean {
  return plan.items.some(
    (item) => item.selected && item.status === "conflict" && item.decision === "undecided",
  );
}

export function uploadableFolderImportItems(plan: FolderImportPlan): FolderImportItem[] {
  const conflictsUnresolved = hasUnresolvedFolderImportConflicts(plan);
  return plan.items.filter((item) =>
    item.selected && (
      item.status === "eligible"
      || (!conflictsUnresolved && item.status === "conflict" && item.decision === "upload")
    ),
  );
}
