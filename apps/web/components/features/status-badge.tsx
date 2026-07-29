import {
  Check,
  CircleDashed,
  Loader2,
  Pause,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  documentActivityLabelKey,
  type DocumentActivityPhase,
} from "@/lib/document-activity";
import type { DocumentStatus } from "@/lib/types";

const MAP: Record<
  DocumentActivityPhase,
  {
    variant: "outline" | "secondary" | "success" | "destructive";
    icon: typeof Check;
    spin?: boolean;
  }
> = {
  pending: { variant: "outline", icon: CircleDashed },
  loading: { variant: "secondary", icon: Loader2, spin: true },
  extracting: { variant: "secondary", icon: Loader2, spin: true },
  paused: { variant: "outline", icon: Pause },
  ready: { variant: "success", icon: Check },
  failed: { variant: "destructive", icon: XCircle },
  requeueing: { variant: "secondary", icon: RefreshCw, spin: true },
  pausing: { variant: "secondary", icon: Loader2, spin: true },
  resuming: { variant: "secondary", icon: Loader2, spin: true },
  deleting: { variant: "secondary", icon: Trash2 },
  "waiting-retry": { variant: "outline", icon: RefreshCw, spin: true },
};

export function DocStatusBadge({ status }: { status: DocumentStatus }) {
  return <DocumentActivityBadge phase={status} />;
}

export function DocumentActivityBadge({ phase }: { phase: DocumentActivityPhase }) {
  const t = useTranslations("DocumentStatus");
  const c = MAP[phase] ?? MAP.pending;
  const Icon = c.icon;
  return (
    <Badge variant={c.variant}>
      <Icon className={cn("size-3", c.spin && "animate-spin")} />
      {t(documentActivityLabelKey(phase))}
    </Badge>
  );
}
