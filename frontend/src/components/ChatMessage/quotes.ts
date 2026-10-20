/**
 * Quote-replies travel as plain markdown blockquotes at the head of the user's
 * message, so the agent reads them as ordinary text and a reloaded thread
 * carries them without any extra stored field.
 */

export function quoteBlock(quote: string): string {
  return quote
    .split("\n")
    .map((line) => (line.trim() ? `> ${line}` : ">"))
    .join("\n")
}

export function composeQuotedMessage(quotes: string[], reply: string): string {
  if (quotes.length === 0) return reply
  return [...quotes.map(quoteBlock), reply].join("\n\n")
}

export interface QuotedMessage {
  quotes: string[]
  body: string
}

/** Splits the leading blockquotes off a user message; a blank line separates quotes. */
export function splitQuotedMessage(text: string): QuotedMessage {
  const lines = text.split("\n")
  const quotes: string[] = []
  let current: string[] | null = null
  let i = 0
  for (; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith(">")) {
      current ??= []
      current.push(line.replace(/^> ?/, ""))
      continue
    }
    if (current && line.trim() === "") {
      quotes.push(current.join("\n"))
      current = null
      continue
    }
    break
  }
  if (current) quotes.push(current.join("\n"))
  if (quotes.length === 0) return { quotes: [], body: text }
  return { quotes, body: lines.slice(i).join("\n") }
}
