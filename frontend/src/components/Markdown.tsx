import { Fragment } from "react";

import {
  type InlineSpan,
  type ListItem,
  type MarkdownBlock,
  parseMarkdown,
} from "../lib/miniMarkdown";

/**
 * Renders the markdown subset described in `lib/miniMarkdown.ts`.
 *
 * Everything renders as React elements built from parsed spans -- there is no
 * `dangerouslySetInnerHTML` here, and there should not be one: the report
 * interpolates filenames and tool parameters that came from disk, and those
 * are data.
 */
function Inline({ spans }: { spans: InlineSpan[] }) {
  return (
    <>
      {spans.map((span, i) => {
        switch (span.kind) {
          case "bold":
            return (
              <strong key={i}>
                <Inline spans={span.children} />
              </strong>
            );
          case "italic":
            return (
              <em key={i}>
                <Inline spans={span.children} />
              </em>
            );
          case "code":
            return (
              <code key={i} className="md-code">
                {span.text}
              </code>
            );
          default:
            return <Fragment key={i}>{span.text}</Fragment>;
        }
      })}
    </>
  );
}

function Items({ items }: { items: ListItem[] }) {
  return (
    <ul className="md-list">
      {items.map((item, i) => (
        <li key={i}>
          <Inline spans={item.spans} />
          {item.children.length > 0 && <Items items={item.children} />}
        </li>
      ))}
    </ul>
  );
}

function Block({ block }: { block: MarkdownBlock }) {
  switch (block.kind) {
    case "heading":
      // Level is relative to the section the report sits inside, so the
      // report's own `##` renders as an h3 rather than competing with the
      // panel's headings.
      return block.level === 2 ? (
        <h3 className="md-h1">
          <Inline spans={block.spans} />
        </h3>
      ) : (
        <h4 className="md-h2">
          <Inline spans={block.spans} />
        </h4>
      );
    case "list":
      return <Items items={block.items} />;
    default:
      return (
        <p className="md-p">
          <Inline spans={block.spans} />
        </p>
      );
  }
}

export function Markdown({ source }: { source: string }) {
  const blocks = parseMarkdown(source);
  return (
    <div className="md">
      {blocks.map((block, i) => (
        <Block key={i} block={block} />
      ))}
    </div>
  );
}
