"use client";

import { useTranslations } from "next-intl";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  getSearchStrategy,
  SEARCH_STRATEGY_ICONS,
  type SearchStrategy,
} from "@/lib/retrieval-config";
import { cn } from "@/lib/utils";

/** Compact "default retrieval mode" marker shown next to a source's name. */
export function RetrievalModePill({
  strategy,
  className,
  tooltipSide = "bottom",
}: {
  strategy: SearchStrategy;
  className?: string;
  tooltipSide?: React.ComponentProps<typeof TooltipContent>["side"];
}) {
  const t = useTranslations("RetrievalModes");
  const strategies = useTranslations("SearchStrategies");
  const mode = getSearchStrategy(strategy);
  const Icon = SEARCH_STRATEGY_ICONS[mode.value];
  const label = strategies(mode.labelKey);
  const tooltip = t("pillTooltip", { label });

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="source-default-mode"
          data-strategy={mode.value}
          aria-label={tooltip}
          className={cn(
            "inline-flex max-w-full shrink-0 items-center gap-1 rounded-full border bg-background px-1.5 py-0.5 text-[10px] font-normal leading-none text-muted-foreground",
            className,
          )}
        >
          <Icon className="size-3 shrink-0" />
          <span className="truncate">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side={tooltipSide} className="max-w-[240px] text-left">
        {tooltip}
      </TooltipContent>
    </Tooltip>
  );
}
