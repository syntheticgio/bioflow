/**
 * A parser for the markdown subset the provenance report emits.
 *
 * Deliberately not a general markdown implementation. `provenance_report.py`
 * renders a closed grammar -- `##`/`###` headings, `-` list items with at most
 * one level of `  -` nesting, and inline `**bold**`, `_italic_`, `` `code` ``
 * -- and nothing else reaches this. Parsing exactly that grammar keeps the
 * renderer dependency-free and, more usefully, keeps anything unexpected in
 * the input rendering as literal text rather than as markup: the report is a
 * document a user cites, so a stray asterisk in a filename must not silently
 * restyle half a line.
 *
 * Inline text is returned as spans rather than HTML for the same reason --
 * nothing here builds a string that a renderer would then have to trust.
 */

/**
 * Emphasis nests, code does not.
 *
 * `**bold**` and `_italic_` carry child spans because the report puts
 * filenames inside them -- the branch line is one italic sentence containing
 * two `` `code` `` filenames, and a flat model renders its markers as
 * literal underscores. A code span's body is always literal text: inside
 * backticks a `_` is an underscore in a filename, not emphasis.
 */
export type InlineSpan =
  | { kind: "text"; text: string }
  | { kind: "bold"; children: InlineSpan[] }
  | { kind: "italic"; children: InlineSpan[] }
  | { kind: "code"; text: string };

export type MarkdownBlock =
  | { kind: "heading"; level: 2 | 3; spans: InlineSpan[] }
  | { kind: "paragraph"; spans: InlineSpan[] }
  | { kind: "list"; items: ListItem[] };

export interface ListItem {
  spans: InlineSpan[];
  children: ListItem[];
}

// One alternation per inline form. Each is non-greedy and requires a
// non-empty body, so `**` alone or an unclosed `` ` `` falls through to
// literal text instead of swallowing the rest of the line.
//
// The italic arm carries an extra restriction that the others do not need:
// its delimiter must not touch a word character on the outside. Nearly every
// identifier in this report is snake_case -- job types (`download_sra_run`),
// every parameter name (`min_length`, `sliding_window_size`), and many
// filenames -- and a bare `_(.+?)_` happily spans from one word's underscore
// to the next, rendering `download_sra_run` as "download*sra*run" and
// scrambling any line with two such names in it. Intra-word underscores are
// literal here, matching how real markdown treats them.
// One pattern per inline form, tried independently rather than as a single
// alternation: the winner is whichever matches earliest in the line, so an
// italic sentence containing backticked filenames is recognised as italic
// (with the code nested inside it) instead of losing to the code span that
// happens to sit in its middle.
const BOLD = /\*\*(.+?)\*\*/;
const CODE = /`([^`]+)`/;
// The italic delimiter must not touch a word character on the outside.
// Nearly every identifier in this report is snake_case -- job types
// (`download_sra_run`), every parameter name (`min_length`,
// `sliding_window_size`), and many filenames -- and a bare `_(.+?)_` happily
// spans from one word's underscore to the next, rendering `download_sra_run`
// as "download*sra*run" and scrambling any line holding two such names.
// Intra-word underscores stay literal, matching how real markdown treats
// them.
// The body may itself contain underscores -- the branch sentence wraps
// filenames like `DRR1066343_1.fastq` -- so it is `.+?` rather than `[^_]+`.
// What keeps that from running away is the boundary assertion on each
// delimiter: an underscore only opens or closes emphasis when its outer side
// is not a word character. The underscore inside `min_length` has letters on
// both sides and so is neither, which is what makes a snake_case name
// literal while `_All facts recorded._` still reads as emphasis.
const ITALIC = /(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])/;

/** Split one line of text into styled spans. */
export function parseInline(line: string): InlineSpan[] {
  const spans: InlineSpan[] = [];
  let rest = line;

  for (;;) {
    const bold = BOLD.exec(rest);
    const code = CODE.exec(rest);
    const italic = ITALIC.exec(rest);

    const best = [bold, code, italic]
      .filter((m): m is RegExpExecArray => m !== null)
      .sort((a, b) => a.index - b.index)[0];
    if (!best) break;

    if (best.index > 0) {
      spans.push({ kind: "text", text: rest.slice(0, best.index) });
    }

    if (best === code) {
      // Literal by definition -- see InlineSpan.
      spans.push({ kind: "code", text: best[1] });
    } else {
      spans.push({
        kind: best === bold ? "bold" : "italic",
        children: parseInline(best[1]),
      });
    }

    rest = rest.slice(best.index + best[0].length);
  }

  if (rest) {
    spans.push({ kind: "text", text: rest });
  }

  return spans;
}

const HEADING = /^(#{2,3})\s+(.*)$/;
const BULLET = /^(\s*)-\s+(.*)$/;

/**
 * Parse the report into blocks.
 *
 * Consecutive bullets become one list; a bullet indented by two or more
 * spaces attaches to the preceding top-level item, which is the only nesting
 * the report produces (the `  - Parameters: ...` continuation line).
 */
export function parseMarkdown(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let list: ListItem[] | null = null;

  const closeList = () => {
    if (list) {
      blocks.push({ kind: "list", items: list });
      list = null;
    }
  };

  for (const raw of source.split("\n")) {
    const line = raw.trimEnd();

    if (!line.trim()) {
      closeList();
      continue;
    }

    const bullet = BULLET.exec(line);
    if (bullet) {
      const indented = bullet[1].length >= 2;
      const item: ListItem = { spans: parseInline(bullet[2]), children: [] };

      if (!list) {
        list = [];
      }
      // An indented bullet with no parent yet (malformed input) is treated as
      // top-level rather than dropped.
      const parent = indented ? list[list.length - 1] : undefined;
      if (parent) {
        parent.children.push(item);
      } else {
        list.push(item);
      }
      continue;
    }

    closeList();

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length === 2 ? 2 : 3,
        spans: parseInline(heading[2]),
      });
      continue;
    }

    blocks.push({ kind: "paragraph", spans: parseInline(line.trim()) });
  }

  closeList();
  return blocks;
}
