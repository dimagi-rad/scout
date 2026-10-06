import { describe, expect, it } from "vitest"
import { csvFilename, slugify, toCsv } from "./csv"

describe("toCsv", () => {
  it("writes header and rows with CRLF line endings", () => {
    expect(toCsv(["a", "b"], [[1, "x"]])).toBe("a,b\r\n1,x\r\n")
  })

  it("quotes commas, quotes and newlines (RFC 4180)", () => {
    expect(toCsv(["c"], [["a,b"], ['say "hi"'], ["l1\nl2"], ["l1\r\nl2"]])).toBe(
      'c\r\n"a,b"\r\n"say ""hi"""\r\n"l1\nl2"\r\n"l1\r\nl2"\r\n',
    )
  })

  it("renders null and undefined as empty cells", () => {
    expect(toCsv(["a", "b", "c"], [[null, undefined, 0]])).toBe("a,b,c\r\n,,0\r\n")
  })

  it("JSON-encodes objects and quotes the result", () => {
    expect(toCsv(["j"], [[{ k: [1, 2] }]])).toBe('j\r\n"{""k"":[1,2]}"\r\n')
  })

  it.each(["=SUM(A1)", "+1+1", "-2+3", "@cmd", "\tx", "\rx"])(
    "neutralises formula-leading string %j",
    (cell) => {
      const out = toCsv(["c"], [[cell]])
      expect(out.split("\r\n")[1].replace(/^"/, "")).toMatch(/^'/)
    },
  )

  it("also protects header cells", () => {
    expect(toCsv(["=evil()"], [])).toBe("'=evil()\r\n")
  })

  it("leaves real negative numbers alone", () => {
    expect(toCsv(["n"], [[-5], [-1.5]])).toBe("n\r\n-5\r\n-1.5\r\n")
  })

  it("leaves numeric strings (Postgres numeric) alone", () => {
    expect(toCsv(["n"], [["-12.50"], ["+5"], ["-1e3"], [".5"]])).toBe("n\r\n-12.50\r\n+5\r\n-1e3\r\n.5\r\n")
  })

  it("still neutralises non-numeric text starting with a minus", () => {
    expect(toCsv(["n"], [["-1+1"], ["-"]])).toBe("n\r\n'-1+1\r\n'-\r\n")
  })

  it("neutralises fullwidth formula starters", () => {
    expect(toCsv(["c"], [["＝1+1"]])).toBe("c\r\n'＝1+1\r\n")
  })

  it("quotes after prefixing when needed", () => {
    expect(toCsv(["c"], [["=a,b"]])).toBe("c\r\n\"'=a,b\"\r\n")
  })
})

describe("csvFilename", () => {
  it("slugifies the title and appends a timestamp", () => {
    const now = new Date("2026-10-06T12:34:56Z")
    expect(csvFilename("Visits by Month: Q3!", now)).toBe("visits-by-month-q3-20261006-123456.csv")
  })

  it("falls back to scout-query", () => {
    const now = new Date("2026-10-06T12:34:56Z")
    expect(csvFilename("", now)).toBe("scout-query-20261006-123456.csv")
    expect(csvFilename(undefined, now)).toBe("scout-query-20261006-123456.csv")
    expect(csvFilename("???", now)).toBe("scout-query-20261006-123456.csv")
  })

  it("slugify trims and caps length", () => {
    expect(slugify("  --Hello  World-- ")).toBe("hello-world")
    expect(slugify("a".repeat(100)).length).toBe(60)
  })
})
