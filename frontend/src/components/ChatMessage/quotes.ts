/**
 * Quote-replies travel as plain markdown blockquotes ahead of the user's reply,
 * so the agent reads them as ordinary text and a reloaded thread carries them
 * without any extra stored field.
 */

function quoteBlock(quote: string): string {
  return quote
    .split("\n")
    .map((line) => (line.trim() ? `> ${line}` : ">"))
    .join("\n")
}

// A reply starting with ">" would read as one more quote; markdown's escape keeps it the reply.
const ESCAPE = "\\"

export function composeQuotedMessage(quotes: string[], reply: string): string {
  if (quotes.length === 0) return reply
  const safeReply = reply.startsWith(">") ? `${ESCAPE}${reply}` : reply
  return [...quotes.map(quoteBlock), safeReply].join("\n\n")
}

export type MessageSegment = { kind: "quote" | "text"; text: string }

/**
 * Splits a user message into quotes and text. A quote is a run of `>` lines that
 * starts a paragraph and is followed by a blank line and, somewhere after it,
 * reply text — the shape composeQuotedMessage writes. Anything else stays text,
 * so a message like ">50% coverage?" is not taken for a quote.
 */
export function messageSegments(text: string): MessageSegment[] {
  const lines = text.split("\n")
  const segments: MessageSegment[] = []
  let textLines: string[] = []
  const flushText = () => {
    if (textLines.length) segments.push({ kind: "text", text: textLines.join("\n") })
    textLines = []
  }
  let i = 0
  while (i < lines.length) {
    const startsParagraph = i === 0 || lines[i - 1].trim() === ""
    if (startsParagraph && lines[i].startsWith(">")) {
      let end = i
      while (end < lines.length && lines[end].startsWith(">")) end++
      const followedByReply =
        end < lines.length
        && lines[end].trim() === ""
        && lines.slice(end + 1).some((line) => line.trim() && !line.startsWith(">"))
      if (followedByReply) {
        if (textLines.length && textLines[textLines.length - 1].trim() === "") textLines.pop()
        flushText()
        segments.push({
          kind: "quote",
          text: lines.slice(i, end).map((line) => line.replace(/^> ?/, "")).join("\n"),
        })
        i = end + 1
        // Taken as text here: re-scanned, the unescaped line would read as another quote.
        if (lines[i]?.startsWith(`${ESCAPE}>`)) {
          textLines.push(lines[i].slice(ESCAPE.length))
          i++
        }
        continue
      }
    }
    textLines.push(lines[i])
    i++
  }
  flushText()
  return segments
}

export interface QuotedMessage {
  quotes: string[]
  body: string
}

/** The quotes leading a message, and the text after them. */
export function splitQuotedMessage(text: string): QuotedMessage {
  const segments = messageSegments(text)
  const quotes: string[] = []
  let i = 0
  for (; i < segments.length && segments[i].kind === "quote"; i++) quotes.push(segments[i].text)
  if (quotes.length === 0) return { quotes, body: text }
  return {
    quotes,
    body: segments
      .slice(i)
      .map((s) => (s.kind === "quote" ? quoteBlock(s.text) : s.text))
      .join("\n\n"),
  }
}
