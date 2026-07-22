import { describe, expect, it } from "vitest";

import {
  citationCopy,
  cleanCitationText,
  stripCitationTransportTokens,
} from "./citation-presentation";
import type { Citation } from "./types";

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    n: 1,
    kind: "internal",
    chunk_id: "chunk-1",
    heading: "发布记录",
    snippet: "这只是检索命中的原文片段，不能自动成为事件标题或摘要。后续正文。",
    score: 0.9,
    source_id: "source-1",
    source_name: "项目资料",
    ...overrides,
  };
}

describe("citation presentation", () => {
  it("uses only the first real event reference for an internal title and body", () => {
    expect(
      citationCopy(
        citation({
          event_refs: [
            {
              id: "event-1",
              title: "AI",
              content: "产品已经完成准备，并正式进入公开测试。",
              summary: "不应展示的事件摘要。",
            },
            {
              id: "event-2",
              title: "不应默认展示的第二事件",
              content: "第二事件正文。",
              summary: "第二事件摘要。",
            },
          ],
        }),
        1,
      ),
    ).toEqual({
      mode: "event",
      title: "AI",
      body: "产品已经完成准备，并正式进入公开测试。",
      meta: "",
    });
  });

  it("falls back to a neutral knowledge source and keeps heading/source as metadata", () => {
    const copy = citationCopy(citation(), 1);

    expect(copy).toEqual({
      mode: "source_only",
      title: "知识库来源 1",
      body: "",
      meta: "项目资料 · 章节：发布记录",
    });
    expect(copy.title).not.toContain("检索命中");
    expect(copy.body).not.toContain("检索命中");
  });

  it("never treats a legacy internal summary or heading as event metadata", () => {
    const copy = citationCopy(
      citation({
        heading: "pdf",
        source_name: "pdf",
        summary: "旧客户端从片段首句生成的摘要。",
        snippet: "产品介绍 Zleap 智跃一体机。后续正文。",
      }),
      1,
    );

    expect(copy).toEqual({
      mode: "source_only",
      title: "知识库来源 1",
      body: "",
      meta: "pdf",
    });
  });

  it("uses only explicit external title, summary, source and URL metadata", () => {
    expect(
      citationCopy(
        citation({
          kind: "external",
          title: "官方发布说明",
          summary: "官方确认新版本已经发布。",
          source: "Example Research",
          url: "https://news.example.com/releases/1",
          heading: "不应使用的 heading",
          source_name: "不应使用的 source_name",
          snippet: "外部工具返回的更长正文片段。",
        }),
        1,
      ),
    ).toEqual({
      mode: "external",
      title: "官方发布说明",
      body: "官方确认新版本已经发布。",
      meta: "Example Research · news.example.com",
    });
  });

  it("does not promote an external snippet when explicit title and summary are missing", () => {
    expect(
      citationCopy(
        citation({
          kind: "external",
          title: null,
          summary: undefined,
          source: null,
          url: "https://www.example.com/article",
          snippet: "不能冒充外部标题或摘要的正文。",
        }),
        2,
      ),
    ).toEqual({
      mode: "external",
      title: "example.com",
      body: "",
      meta: "",
    });
  });

  it("cleans malformed citation tokens within fields without moving text between fields", () => {
    const copy = citationCopy(
      citation({
        event_refs: [
          {
            title: "## 官方更新 □cite□turn17view2",
            content: "**已经发布** □cite□turn17view4",
            summary: "不应展示的摘要",
          },
        ],
        snippet: "`完整片段` □cite□turn17view5",
      }),
      1,
    );

    expect(copy).toMatchObject({
      title: "官方更新",
      body: "已经发布",
    });
    expect(cleanCitationText("## 执行摘要\n**核心结论**见[报告](https://example.com)。")).toBe(
      "执行摘要 核心结论见报告。",
    );
    expect(
      cleanCitationText(
        "真实原文 \ue200cite\ue202turn8view5\ue202turn19view0\ue201 后续内容",
      ),
    ).toBe("真实原文 后续内容");
    expect(
      stripCitationTransportTokens(
        "第一段 \ue200cite\ue202turn8view5\ue201\n\n第二段 **保留原文格式**",
      ),
    ).toBe("第一段\n\n第二段 **保留原文格式**");
  });

  it("provides a stable source-only fallback when all metadata is empty", () => {
    expect(
      citationCopy(
        citation({ heading: "", source_name: null, snippet: "", n: 3 }),
        1,
      ),
    ).toEqual({
      mode: "source_only",
      title: "知识库来源 3",
      body: "",
      meta: "",
    });
  });

  it("does not fall back to an event summary or source chunk when content is missing", () => {
    const copy = citationCopy(
      citation({
        snippet: "检索分块正文",
        event_refs: [{ title: "真实事项", summary: "压缩摘要" }],
      }),
      1,
    );

    expect(copy).toMatchObject({
      mode: "event",
      title: "真实事项",
      body: "",
    });
  });
});
