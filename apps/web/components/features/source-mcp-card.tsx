"use client";

import * as React from "react";
import { Plug } from "lucide-react";
import { useTranslations } from "next-intl";

import { api } from "@/lib/api";
import type { SourceMcpDescriptor } from "@/lib/types";
import { CodeBlock } from "@/components/features/code-block";
import { CopyButton } from "@/components/features/copy-button";
import { McpToolList } from "@/components/features/mcp-tool-list";
import { mcpServerKey } from "@/lib/mcp-server-key";

export function SourceMcpCard({ sourceId }: { sourceId: string }) {
  const t = useTranslations("SourceMcp");
  const [desc, setDesc] = React.useState<SourceMcpDescriptor | null>(null);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    api
      .sourceMcp(sourceId)
      .then(setDesc)
      .catch(() => setFailed(true));
  }, [sourceId]);

  if (failed || !desc) return null;

  const serverKey = mcpServerKey(
    desc.source_name,
    `sag-source-${desc.source_id}`,
  );
  const stdioSnippet = JSON.stringify(
    {
      mcpServers: {
        [serverKey]: {
          command: desc.stdio.command,
          args: desc.stdio.args,
          env: desc.stdio.env,
        },
      },
    },
    null,
    2,
  );

  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <Plug className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-medium">{t("title")}</h2>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("description")}
      </p>
      <McpToolList tools={desc.tool_details} />

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-muted-foreground">{t("http")}</span>
          <CopyButton text={desc.http.url} label="URL" />
        </div>
        <CodeBlock>{desc.http.url}</CodeBlock>
        <p className="text-[11px] text-muted-foreground">{desc.http.note}</p>
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-muted-foreground">{t("stdio")}</span>
          <CopyButton text={stdioSnippet} label={t("configuration")} />
        </div>
        <CodeBlock>{stdioSnippet}</CodeBlock>
        <p className="text-[11px] text-muted-foreground">{desc.stdio.note}</p>
      </div>
    </section>
  );
}
