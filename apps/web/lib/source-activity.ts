import type { Source } from "./types";

export function isSourceDeleting(source: Pick<Source, "status">): boolean {
  return source.status === "deleting" || source.status === "delete_failed";
}
