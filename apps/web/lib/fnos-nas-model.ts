import type {
  FnOSNasImportAccepted,
  FnOSNasImportProgress,
  FnOSNasScanFile,
  FnOSNasScanResult,
  FnOSNasStatus,
} from "./types";

export type FnOSNasImportStage =
  | "idle"
  | "scanning"
  | "loaded"
  | "importing"
  | "complete"
  | "error";

export interface FnOSNasImportState {
  status: FnOSNasStatus | null;
  stage: FnOSNasImportStage;
  scan: FnOSNasScanResult | null;
  selection: ReadonlySet<string>;
  query: string;
  typeFilter: string;
  stateFilter: string;
  page: number;
  pageSize: number;
  importAccepted: FnOSNasImportAccepted | null;
  importProgress: FnOSNasImportProgress | null;
  error: string | null;
}

export const initialFnOSNasImportState: FnOSNasImportState = {
  status: null,
  stage: "idle",
  scan: null,
  selection: new Set(),
  query: "",
  typeFilter: "all",
  stateFilter: "all",
  page: 1,
  pageSize: 50,
  importAccepted: null,
  importProgress: null,
  error: null,
};

export type FnOSNasImportAction =
  | { type: "status.loaded"; status: FnOSNasStatus }
  | { type: "scan.started" }
  | { type: "scan.cancelled" }
  | { type: "scan.loaded"; result: FnOSNasScanResult }
  | { type: "scan.failed"; message: string }
  | {
      type: "filter.changed";
      filter: "query" | "type" | "state";
      value: string;
    }
  | { type: "page.changed"; page: number }
  | { type: "selection.toggled"; token: string; selected: boolean }
  | { type: "selection.page"; selected: boolean }
  | { type: "selection.all"; selected: boolean }
  | { type: "selection.cleared" }
  | { type: "import.started"; accepted: FnOSNasImportAccepted }
  | { type: "import.progress"; progress: FnOSNasImportProgress }
  | { type: "import.failed"; message: string }
  | { type: "reset" };

type EligibleFnOSNasScanFile = FnOSNasScanFile & { selection_token: string };

function eligible(file: FnOSNasScanFile): file is EligibleFnOSNasScanFile {
  return (
    (file.state === "new" || file.state === "changed") &&
    typeof file.selection_token === "string" &&
    file.selection_token.length > 0
  );
}

export function selectFilteredFiles(state: FnOSNasImportState): FnOSNasScanFile[] {
  const query = state.query.trim().toLocaleLowerCase();
  return (state.scan?.files ?? []).filter((file) => {
    if (query && !file.display_path.toLocaleLowerCase().includes(query)) return false;
    if (state.typeFilter !== "all" && file.extension !== state.typeFilter) return false;
    if (state.stateFilter !== "all" && file.state !== state.stateFilter) return false;
    return true;
  });
}

export function selectPageFiles(state: FnOSNasImportState): FnOSNasScanFile[] {
  const start = (Math.max(1, state.page) - 1) * state.pageSize;
  return selectFilteredFiles(state).slice(start, start + state.pageSize);
}

export function selectEligibleFiles(state: FnOSNasImportState): EligibleFnOSNasScanFile[] {
  return (state.scan?.files ?? []).filter(eligible);
}

export function selectSelectedFiles(state: FnOSNasImportState): EligibleFnOSNasScanFile[] {
  return selectEligibleFiles(state).filter((file) =>
    state.selection.has(file.selection_token),
  );
}

export function selectSelectedTokens(state: FnOSNasImportState): string[] {
  return selectSelectedFiles(state).map((file) => file.selection_token);
}

export function selectImportTotals(state: FnOSNasImportState): {
  files: number;
  bytes: number;
} {
  const files = selectSelectedFiles(state);
  return {
    files: files.length,
    bytes: files.reduce((total, file) => total + file.size_bytes, 0),
  };
}

export function selectPageSelection(state: FnOSNasImportState): {
  checked: boolean;
  indeterminate: boolean;
  eligible: number;
} {
  const files = selectPageFiles(state).filter(eligible);
  const selected = files.filter((file) => state.selection.has(file.selection_token)).length;
  return {
    checked: files.length > 0 && selected === files.length,
    indeterminate: selected > 0 && selected < files.length,
    eligible: files.length,
  };
}

function changeSelection(
  state: FnOSNasImportState,
  files: FnOSNasScanFile[],
  selected: boolean,
): ReadonlySet<string> {
  const next = new Set(state.selection);
  for (const file of files) {
    if (!eligible(file)) continue;
    if (selected) next.add(file.selection_token);
    else next.delete(file.selection_token);
  }
  return next;
}

export function reduceFnOSNasImport(
  state: FnOSNasImportState,
  action: FnOSNasImportAction,
): FnOSNasImportState {
  switch (action.type) {
    case "status.loaded":
      return { ...state, status: action.status };
    case "scan.started":
      return {
        ...state,
        stage: "scanning",
        importAccepted: null,
        importProgress: null,
        error: null,
      };
    case "scan.loaded":
      return {
        ...state,
        stage: "loaded",
        scan: action.result,
        selection: new Set(
          action.result.files
            .filter((file) => eligible(file) && file.selected_by_default)
            .map((file) => file.selection_token as string),
        ),
        page: 1,
        importAccepted: null,
        importProgress: null,
        error: null,
      };
    case "scan.cancelled":
      return { ...state, stage: state.scan ? "loaded" : "idle", error: null };
    case "scan.failed":
      return { ...state, stage: "error", error: action.message };
    case "filter.changed":
      return {
        ...state,
        [action.filter === "type" ? "typeFilter" : action.filter === "state" ? "stateFilter" : "query"]:
          action.value,
        page: 1,
      };
    case "page.changed":
      return { ...state, page: Math.max(1, action.page) };
    case "selection.toggled": {
      const next = new Set(state.selection);
      if (action.selected) next.add(action.token);
      else next.delete(action.token);
      return { ...state, selection: next };
    }
    case "selection.page":
      return {
        ...state,
        selection: changeSelection(state, selectPageFiles(state), action.selected),
      };
    case "selection.all":
      return {
        ...state,
        selection: changeSelection(state, selectFilteredFiles(state), action.selected),
      };
    case "selection.cleared":
      return { ...state, selection: new Set() };
    case "import.started":
      return {
        ...state,
        stage: "importing",
        importAccepted: action.accepted,
        importProgress: null,
        error: null,
      };
    case "import.progress":
      return {
        ...state,
        stage:
          action.progress.status === "succeeded" || action.progress.status === "failed"
            ? "complete"
            : "importing",
        importProgress: action.progress,
      };
    case "import.failed":
      return { ...state, stage: "error", error: action.message };
    case "reset":
      return { ...initialFnOSNasImportState, selection: new Set() };
  }
}
