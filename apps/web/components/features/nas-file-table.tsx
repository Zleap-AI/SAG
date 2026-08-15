"use client";

import { useLocale, useTranslations } from "next-intl";

import { formatBytes, formatDate } from "@/lib/format";
import {
  selectFilteredFiles,
  selectPageFiles,
  selectPageSelection,
  type FnOSNasImportAction,
  type FnOSNasImportState,
} from "@/lib/fnos-nas-model";
import type { FnOSNasFileState, FnOSNasScanFile } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";

const STATE_KEYS = {
  new: "stateNew",
  changed: "stateChanged",
  imported: "stateImported",
  unsupported: "stateUnsupported",
  too_large: "stateTooLarge",
  unreadable: "stateUnreadable",
} as const satisfies Record<FnOSNasFileState, string>;

function canSelect(file: FnOSNasScanFile): file is FnOSNasScanFile & {
  selection_token: string;
} {
  return Boolean(
    file.selection_token && (file.state === "new" || file.state === "changed"),
  );
}

export function NasFileTable({
  state,
  dispatch,
}: {
  state: FnOSNasImportState;
  dispatch: React.Dispatch<FnOSNasImportAction>;
}) {
  const t = useTranslations("FnOSNas");
  const locale = useLocale();
  const filtered = selectFilteredFiles(state);
  const pageFiles = selectPageFiles(state);
  const pageSelection = selectPageSelection(state);
  const extensions = Array.from(
    new Set((state.scan?.files ?? []).map((file) => file.extension).filter(Boolean)),
  ).sort();
  const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize));

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_10rem_10rem]">
        <Input
          value={state.query}
          aria-label={t("search")}
          placeholder={t("search")}
          onChange={(event) =>
            dispatch({ type: "filter.changed", filter: "query", value: event.target.value })
          }
        />
        <select
          value={state.typeFilter}
          aria-label={t("allTypes")}
          className="h-9 rounded-md border bg-background px-3 text-sm"
          onChange={(event) =>
            dispatch({ type: "filter.changed", filter: "type", value: event.target.value })
          }
        >
          <option value="all">{t("allTypes")}</option>
          {extensions.map((extension) => (
            <option key={extension} value={extension}>
              {extension || "—"}
            </option>
          ))}
        </select>
        <select
          value={state.stateFilter}
          aria-label={t("allStates")}
          className="h-9 rounded-md border bg-background px-3 text-sm"
          onChange={(event) =>
            dispatch({ type: "filter.changed", filter: "state", value: event.target.value })
          }
        >
          <option value="all">{t("allStates")}</option>
          {(Object.keys(STATE_KEYS) as FnOSNasFileState[]).map((value) => (
            <option key={value} value={value}>
              {t(STATE_KEYS[value])}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={pageSelection.eligible === 0}
          onClick={() =>
            dispatch({ type: "selection.page", selected: !pageSelection.checked })
          }
        >
          {t("selectPage")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => dispatch({ type: "selection.all", selected: true })}
        >
          {t("selectAll")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => dispatch({ type: "selection.cleared" })}
        >
          {t("clearSelection")}
        </Button>
      </div>

      <div className="max-h-[45vh] overflow-auto rounded-md border">
        <table className="w-full min-w-[680px] text-left text-sm">
          <thead className="sticky top-0 z-10 bg-muted/95 text-xs text-muted-foreground">
            <tr>
              <th className="w-10 px-3 py-2">
                <Checkbox
                  aria-label={t("selectPage")}
                  checked={
                    pageSelection.indeterminate ? "indeterminate" : pageSelection.checked
                  }
                  disabled={pageSelection.eligible === 0}
                  onCheckedChange={(checked) =>
                    dispatch({ type: "selection.page", selected: checked === true })
                  }
                />
              </th>
              <th className="px-3 py-2 font-medium">{t("file")}</th>
              <th className="w-24 px-3 py-2 font-medium">{t("size")}</th>
              <th className="w-36 px-3 py-2 font-medium">{t("modified")}</th>
              <th className="w-32 px-3 py-2 font-medium">{t("state")}</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {pageFiles.map((file) => {
              const selectable = canSelect(file);
              const stateLabel = t(STATE_KEYS[file.state]);
              return (
                <tr key={`${file.display_path}:${file.modified_at}`}>
                  <td className="px-3 py-2 align-top">
                    <Checkbox
                      aria-label={`${file.display_path} · ${stateLabel}`}
                      checked={
                        selectable ? state.selection.has(file.selection_token) : false
                      }
                      disabled={!selectable}
                      title={!selectable ? stateLabel : undefined}
                      onCheckedChange={(checked) => {
                        if (!selectable) return;
                        dispatch({
                          type: "selection.toggled",
                          token: file.selection_token,
                          selected: checked === true,
                        });
                      }}
                    />
                  </td>
                  <td className="min-w-0 px-3 py-2">
                    <div className="max-w-md break-all font-medium">
                      {file.display_path}
                    </div>
                  </td>
                  <td className="px-3 py-2 tabular-nums text-muted-foreground">
                    {formatBytes(file.size_bytes, locale)}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {formatDate(file.modified_at, undefined, { dateStyle: "medium" }, locale)}
                  </td>
                  <td className="px-3 py-2">
                    <Badge
                      variant={
                        file.state === "new" || file.state === "changed"
                          ? "success"
                          : "secondary"
                      }
                    >
                      {stateLabel}
                    </Badge>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={state.page <= 1}
          onClick={() => dispatch({ type: "page.changed", page: state.page - 1 })}
        >
          {t("previousPage")}
        </Button>
        <span>{t("page", { page: state.page, pages })}</span>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={state.page >= pages}
          onClick={() => dispatch({ type: "page.changed", page: state.page + 1 })}
        >
          {t("nextPage")}
        </Button>
      </div>
    </div>
  );
}
