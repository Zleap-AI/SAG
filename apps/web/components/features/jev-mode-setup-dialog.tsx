"use client";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { JevQuickSetup, useJevQuickSetup } from "./jev-quick-setup";

export function JevModeSetupDialog({ onConfigured, onClose }: {
  onConfigured: () => void; onClose: () => void;
}) {
  const t = useTranslations("RetrievalModes");
  const form = useJevQuickSetup({ enabled: true });
  async function save() {
    try { await form.save(); onConfigured(); } catch { /* The form shows the failure inline. */ }
  }
  return <Dialog open onOpenChange={open => { if (!open && !form.saving) onClose(); }}>
    <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg">
      <DialogHeader><DialogTitle>{t("jevSetupTitle")}</DialogTitle>
        <DialogDescription>{t("jevSetupDescription")}</DialogDescription></DialogHeader>
      <JevQuickSetup setup={form} idPrefix="retrieval-mode-setup" />
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose} disabled={form.saving}>{t("cancelSetup")}</Button>
        <Button type="button" onClick={() => void save()} disabled={!form.ready || form.saving || form.testing}>
          {t(form.saving ? "savingSetup" : "saveAndUse")}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
