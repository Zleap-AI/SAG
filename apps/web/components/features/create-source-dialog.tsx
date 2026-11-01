"use client";

import * as React from "react";
import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";

import type { Source } from "@/lib/types";
import { SourceCreateForm } from "@/components/features/source-create-form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

export function CreateSourceDialog({
  onCreated,
  trigger,
}: {
  onCreated: (s: Source) => void;
  trigger?: React.ReactNode;
}) {
  const t = useTranslations("SourceForm");
  const [open, setOpen] = React.useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button>
            <Plus className="size-4" />
            {t("new")}
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("new")}</DialogTitle>
          <DialogDescription>{t("dialogDescription")}</DialogDescription>
        </DialogHeader>

        <SourceCreateForm
          onCreated={(source) => {
            onCreated(source);
            setOpen(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
