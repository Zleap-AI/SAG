/** Wake open document details after a control action, including paused documents. */
export const DOCUMENT_CHANGED_EVENT = "sag:document-changed";

export function notifyDocumentChanged(sourceId: string, documentId: string) {
  window.dispatchEvent(new CustomEvent(DOCUMENT_CHANGED_EVENT, { detail: { sourceId, documentId } }));
}
