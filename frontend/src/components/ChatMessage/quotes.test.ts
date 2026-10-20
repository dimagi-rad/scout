import { describe, expect, it } from "vitest"
import { composeQuotedMessage, messageSegments, splitQuotedMessage } from "./quotes"

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

  it("does not take a message that merely starts with > for a quote", () => {
    for (const text of [">50% coverage sites last month?", "> just this", "> 5 visits\n\n> more"]) {
      expect(splitQuotedMessage(text)).toEqual({ quotes: [], body: text })
    }
  })

  it("finds quotes later in a message, as when a quote-reply joins a held request", () => {
    const joined = `How many visits?\n\n${composeQuotedMessage(["42 visits"], "Only Kenya")}`
    expect(messageSegments(joined)).toEqual([
      { kind: "text", text: "How many visits?" },
      { kind: "quote", text: "42 visits" },
      { kind: "text", text: "Only Kenya" },
    ])
  })

  it("gives back the body after the leading quotes, later quotes intact", () => {
    const text = composeQuotedMessage(["a"], `x\n\n${composeQuotedMessage(["b"], "y")}`)
    expect(splitQuotedMessage(text)).toEqual({ quotes: ["a"], body: "x\n\n> b\n\ny" })
  })
})
