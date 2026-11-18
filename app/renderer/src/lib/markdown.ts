// A small, safe Markdown reader for model answers: paragraphs, lists, code blocks,
// bold and inline code. It produces plain data; nothing is ever inserted as HTML.

export type Inline = { kind: "text" | "bold" | "code"; text: string };

export type Block =
  | { kind: "paragraph"; inline: Inline[] }
  | { kind: "heading"; inline: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "code"; text: string; lang?: string };

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;

export function parseInline(text: string): Inline[] {
  const parts: Inline[] = [];
  for (const piece of text.split(INLINE)) {
    if (!piece) continue;
    if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) {
      parts.push({ kind: "bold", text: piece.slice(2, -2) });
    } else if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 2) {
      parts.push({ kind: "code", text: piece.slice(1, -1) });
    } else {
      parts.push({ kind: "text", text: piece });
    }
  }
  return parts;
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  const flush = (): void => {
    if (paragraph.length) blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join("\n")) });
    paragraph = [];
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.trimStart().startsWith("```")) {
      flush();
      const code: string[] = [];
      // The word after the opening fence, if it is a plain language name.
      const info = line.trimStart().slice(3).trim().split(/\s+/)[0] ?? "";
      const lang = /^[\w+#.-]{1,20}$/.test(info) ? info : "";
      index++;
      // An unclosed fence (the answer is still streaming) runs to the end.
      while (index < lines.length && !lines[index].trimStart().startsWith("```")) code.push(lines[index++]);
      blocks.push({ kind: "code", text: code.join("\n"), ...(lang ? { lang } : {}) });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "heading", inline: parseInline(heading[1]) });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      flush();
      const ordered = numbered !== null;
      const item = parseInline((bullet ?? numbered)![1]);
      const last = blocks.at(-1);
      if (last?.kind === "list" && last.ordered === ordered) last.items.push(item);
      else blocks.push({ kind: "list", ordered, items: [item] });
      continue;
    }
    if (line.trim() === "") flush();
    else paragraph.push(line);
  }
  flush();
  return blocks;
}
