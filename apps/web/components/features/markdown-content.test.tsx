import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import messages from "@/messages/zh-CN.json";
import { ChunkedMarkdown, MarkdownContent } from "./markdown-content";

function renderMarkdown(content: string) {
  return renderToStaticMarkup(
    <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
      <MarkdownContent content={content} />
    </NextIntlClientProvider>,
  );
}

describe("MarkdownContent math rendering", () => {
  it("renders the inline and display LaTeX used in issue #138", () => {
    const content = String.raw`行内公式：$A$。

$$
\frac{A}{1 + \frac{1}{n}} \times \frac{1}{n} = \frac{A}{n+1}
$$

$A^2 + A_i + \Delta + \alpha + \beta + \text{增长率 } 5.2% = 0.052$`;

    const html = renderMarkdown(content);

    expect(html).toContain('class="katex"');
    expect(html).toContain('class="katex-display"');
    expect(html).toContain("<mfrac>");
    expect(html).toContain("<msup>");
    expect(html).toContain("<msub>");
    expect(html).toContain("Δ");
    expect(html).toContain("α");
    expect(html).toContain("β");
    expect(html).toContain("增长率");
    expect(html).toContain("<mn>0.052</mn>");
  });

  it("keeps code examples literal and degrades invalid LaTeX without crashing", () => {
    const html = renderMarkdown("代码：`$A^2$`\n\n无效公式：$\\notARealCommand{x}$");

    expect(html).toContain("<code>$A^2$</code>");
    expect(html).toContain('mathcolor="#cc0000"');
    expect(html).toContain("\\notARealCommand{x}");
  });

  it("keeps rendering after repeated percent signs and even backslashes", () => {
    const html = renderMarkdown(String.raw`$12%% = 0.12$

$7 \\% = 0.34$`);

    expect(html).toContain("<mn>0.12</mn>");
    expect(html).toContain("<mn>0.34</mn>");
  });

  it("renders display math after large-document chunking", () => {
    const content = `${"x".repeat(4985)}\n\n$$\n\\frac{1}{n + 1}\n\n+ z + 1\n$$\n\n${"tail ".repeat(50)}`;
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <ChunkedMarkdown content={content} />
      </NextIntlClientProvider>,
    );

    expect(html).toContain('class="md-block"');
    expect(html).toContain('class="katex-display"');
    expect(html).toContain("<mfrac>");
    expect(html).toContain("<mi>z</mi>");
  });

  it("renders unambiguous parenthesis and bracket math delimiters", () => {
    const html = renderMarkdown(String.raw`行内：\(A^2 + \frac{A}{B}\)。

\[
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
\]`);

    expect(html).toContain('class="katex"');
    expect(html).toContain('class="katex-display"');
    expect(html).toContain("<msup>");
    expect(html).toContain("<mfrac>");
  });

  it("never promotes inline or fenced code to math", () => {
    const html = renderMarkdown("代码：`x`、`a+b`、`key=value`、`a/b`、`<div>`、`$HOME`。\n\n~~~sh\necho $HOME\n~~~");

    expect(html).not.toContain('class="katex"');
    expect(html).toContain("<code>x</code>");
    expect(html).toContain("<code>a+b</code>");
    expect(html).toContain("<code>key=value</code>");
    expect(html).toContain("&lt;div&gt;");
    expect(html).toContain("echo $HOME");
  });

  it("keeps escaped currency and incomplete streaming delimiters literal", () => {
    const html = renderMarkdown(String.raw`金额：\$100 到 \$120；流式半截：\(A^2 + \frac{A}{`);

    expect(html).not.toContain('class="katex"');
    expect(html).toContain("$100 到 $120");
    expect(html).toContain(String.raw`\(A^2 + \frac{A}{`);
  });

  it("keeps ordinary paired currency text out of math rendering", () => {
    const html = renderMarkdown(
      "价格从 $100 上涨到 $120，预算为 $100 + $20；变量：$HOME/$PATH；金额 $100，变量 $HOME；路径 $C:/tmp 和 $HOME/bin。",
    );

    expect(html).not.toContain('class="katex"');
    expect(html).toContain("价格从 $100 上涨到 $120，预算为 $100 + $20");
    expect(html).toContain("$HOME/$PATH");
    expect(html).toContain("金额 $100，变量 $HOME");
    expect(html).toContain("路径 $C:/tmp 和 $HOME/bin");
  });

  it("does not pair display delimiters across code", () => {
    const html = renderMarkdown("\\[\n```txt\n\\]\n```\n正文");

    expect(html).not.toContain('class="katex"');
    expect(html).toContain("<code class=\"language-txt\">\\]");
  });

  it("does not rewrite delimiters in link destinations or even-slash literals", () => {
    const html = renderMarkdown(String.raw`[docs](https://example.test/a\(b\))；字面量：\\(x\)。`);

    expect(html).toContain('href="https://example.test/a(b)"');
    expect(html).toContain(String.raw`\(x\)`);
    expect(html).not.toContain('class="katex"');
  });

  it("does not rewrite delimiters in reference definitions or autolinks", () => {
    const html = renderMarkdown(String.raw`[docs][ref]

[ref]: https://example.test/a\(b\)

<https://example.test/c\(d\)>`);

    expect(html).toContain('href="https://example.test/a(b)"');
    expect(html).toContain('href="https://example.test/c%5C(d%5C)"');
    expect(html).not.toContain('class="katex"');
  });

});
