import { useEffect, useState } from "react";
import { type Block, type Inline, parseMarkdown } from "../lib/markdown";

/** A block of code with a button that copies exactly the code, nothing around it. */
function CodeBlock({ text, lang }: { text: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <div className="codeblock">
      <div className="codebar">
        <span className="codelang">{lang ?? ""}</span>
        <button
          className="plain copy"
          aria-label={copied ? "Copied" : "Copy code"}
          onClick={() => {
            void navigator.clipboard.writeText(text).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>{text}</pre>
    </div>
  );
}

function Spans({ inline }: { inline: Inline[] }) {
  return (
    <>
      {inline.map((part, index) =>
        part.kind === "bold" ? (
          <strong key={index}>{part.text}</strong>
        ) : part.kind === "code" ? (
          <code key={index}>{part.text}</code>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </>
  );
}

function BlockView({ block }: { block: Block }) {
  switch (block.kind) {
    case "code":
      return <CodeBlock text={block.text} lang={block.lang} />;
    case "heading":
      return (
        <h4 className="md-heading">
          <Spans inline={block.inline} />
        </h4>
      );
    case "list": {
      const items = block.items.map((item, index) => (
        <li key={index}>
          <Spans inline={item} />
        </li>
      ));
      return block.ordered ? <ol>{items}</ol> : <ul>{items}</ul>;
    }
    default:
      return (
        <p>
          <Spans inline={block.inline} />
        </p>
      );
  }
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="answer">
      {parseMarkdown(text).map((block, index) => (
        <BlockView key={index} block={block} />
      ))}
    </div>
  );
}
