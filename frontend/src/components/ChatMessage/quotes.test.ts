import { describe, expect, it } from "vitest"
import { composeQuotedMessage, splitQuotedMessage } from "./quotes"

describe("quote-reply message format", () => {
  it("leaves a message without quotes untouched", () => {
    expect(composeQuotedMessage([], "plain")).toBe("plain")
    expect(splitQuotedMessage("plain\n> not a lead quote")).toEqual({
      quotes: [],
      body: "plain\n> not a lead quote",
    })
  })

  it("prepends each quote as its own blockquote", () => {
    expect(composeQuotedMessage(["first point", "second\n\nparagraph"], "Why?")).toBe(
      "> first point\n\n> second\n>\n> paragraph\n\nWhy?",
    )
  })

  it("round-trips multiple multi-line quotes and a multi-line reply", () => {
    const quotes = ["first point", "second\n\nparagraph", "> already quoted"]
    const reply = "Why?\nAnd how?"
    expect(splitQuotedMessage(composeQuotedMessage(quotes, reply))).toEqual({ quotes, body: reply })
  })

  it("reads a quote-only message", () => {
    expect(splitQuotedMessage("> just this")).toEqual({ quotes: ["just this"], body: "" })
  })
})
