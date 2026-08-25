import { describe, expect, it } from "vitest"

import { buildRechartsTree, CHART_PALETTES, compileCompactGraphConfig, formatAxisTick } from "./recharts"

describe("compact Recharts visualization grammar", () => {
  it("compiles a horizontal value-labeled bar with a bounded style", () => {
    const tree = compileCompactGraphConfig({
      chart_type: "bar",
      x_key: "region",
      series: [{ data_key: "visits", label: "Visits" }],
      palette: "sequential",
      legend: "none",
      grid: "horizontal",
      labels: "value",
      orientation: "horizontal",
      y_format: "number_0",
    })

    expect(tree.type).toBe("BarChart")
    expect(tree.palette).toEqual([...CHART_PALETTES.sequential])
    expect(tree.props?.layout).toBe("vertical")
    expect(tree.children?.some((node) => node.type === "Legend")).toBe(false)
    expect(tree.children?.find((node) => node.type === "CartesianGrid")?.props).toMatchObject({
      horizontal: true,
      vertical: false,
    })
    expect(tree.children?.find((node) => node.type === "Bar")?.props).toMatchObject({
      dataKey: "visits",
      fill: CHART_PALETTES.sequential[0],
      radius: [0, 6, 6, 0],
    })
  })

  it("distinguishes pie and donut presets", () => {
    const pie = compileCompactGraphConfig({ chart_type: "pie", y_key: "count" })
    const donut = compileCompactGraphConfig({ chart_type: "donut", y_key: "count" })

    expect(pie.children?.find((node) => node.type === "Pie")?.props?.innerRadius).toBe(0)
    expect(donut.children?.find((node) => node.type === "Pie")?.props?.innerRadius).toBe("52%")
  })

  it("normalizes numeric semantic values before rendering pie sectors", () => {
    const tree = compileCompactGraphConfig({
      chart_type: "donut",
      x_key: "status",
      y_key: "visits_count",
    })
    const rendered = buildRechartsTree(tree, [{ status: "approved", visits_count: "5" }])
    const data = (rendered.props as { data: Array<Record<string, unknown>> }).data

    expect(data[0]).toEqual({ status: "approved", visits_count: 5 })
  })

  it("formats value labels across pie, line, and area charts", () => {
    const pie = compileCompactGraphConfig({ chart_type: "pie", y_key: "amount", labels: "value" })
    const line = compileCompactGraphConfig({ chart_type: "line", y_key: "amount", labels: "value" })
    const area = compileCompactGraphConfig({ chart_type: "area", y_key: "amount", labels: "value" })

    expect(pie.children?.find((node) => node.type === "Pie")?.props?.label).toBeTypeOf("function")
    expect(line.children?.find((node) => node.type === "Line")?.props?.label).toMatchObject({ position: "top" })
    expect(area.children?.find((node) => node.type === "Area")?.props?.label).toMatchObject({ position: "top" })
  })

  it("rejects unsupported compact chart types instead of silently drawing a line", () => {
    expect(() => compileCompactGraphConfig({ chart_type: "radar" })).toThrow(
      'Unsupported compact chart type "radar"',
    )
  })

  it("formats ISO dates as short locale-aware axis labels", () => {
    expect(formatAxisTick("2026-01-05")).toBe("Jan 5")
    expect(formatAxisTick("2026-08-01T00:00:00.000")).toBe("Aug 1")
    expect(formatAxisTick("Approved")).toBe("Approved")
  })

  it("uses whole-number ticks without clipping negative number_0 measures", () => {
    const tree = compileCompactGraphConfig({
      chart_type: "line",
      x_key: "date",
      y_key: "visits",
      y_format: "number_0",
    })

    const yAxis = tree.children?.find((node) => node.type === "YAxis")?.props
    expect(yAxis).toMatchObject({ allowDecimals: false })
    expect(yAxis?.domain).toBeUndefined()
  })

  it("infers whole-number ticks for semantic count series", () => {
    const tree = compileCompactGraphConfig({
      chart_type: "line",
      x_key: "date",
      y_key: "visits_count",
    })

    expect(tree.children?.find((node) => node.type === "YAxis")?.props).toMatchObject({
      allowDecimals: false,
      domain: [0, "dataMax"],
    })
  })

  it("rejects arbitrary DOM-affecting props in raw Recharts trees", () => {
    expect(() => buildRechartsTree({
      type: "BarChart",
      props: { style: { position: "fixed", inset: 0, zIndex: 9999 } },
      children: [],
    }, [])).toThrow('Recharts BarChart prop "style" is not supported')
  })

  it("rejects arbitrary raw chart colors", () => {
    expect(() => buildRechartsTree({
      type: "LineChart",
      children: [{ type: "Line", props: { dataKey: "visits_count", stroke: "red" } }],
    }, [])).toThrow('Recharts Line prop "stroke" must use a Scout chart color token')
  })

  it("allows bounded axis chrome and neutral grid colors", () => {
    expect(() => buildRechartsTree({
      type: "LineChart",
      children: [
        { type: "CartesianGrid", props: { stroke: "var(--border)", vertical: false } },
        { type: "XAxis", props: { dataKey: "date", axisLine: false, tickLine: false } },
        { type: "YAxis", props: { axisLine: false, tickLine: false } },
        { type: "Line", props: { dataKey: "visits_count", stroke: "var(--chart-1)" } },
      ],
    }, [{ date: "2026-08-01", visits_count: 1 }])).not.toThrow()
  })
})
