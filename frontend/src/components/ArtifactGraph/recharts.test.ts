import React from "react"
import { describe, expect, it } from "vitest"

import {
  buildRechartsTree,
  CATEGORY_AXIS_MAX_WIDTH,
  categoryAxisWidth,
  CHART_PALETTES,
  compileCompactGraphConfig,
  prepareCompactGraph,
  formatAxisTick,
  normalizeGraphSeries,
  truncateCategoryLabel,
} from "./recharts"

describe("compact Recharts visualization grammar", () => {
  it.each([
    ["LineChart", "Line"],
    ["AreaChart", "Area"],
    ["BarChart", "Bar"],
    ["PieChart", "Pie"],
    ["ScatterChart", "Scatter"],
  ])("renders %s series completely without data-reveal or resize animations", (chartType, seriesType) => {
    const rendered = buildRechartsTree({
      type: chartType,
      children: [{ type: seriesType }],
    }, [{ count: 4 }])
    const series = React.Children.toArray((rendered.props as { children: React.ReactNode }).children)[0]
    expect(React.isValidElement(series)).toBe(true)
    expect((series as React.ReactElement<{ isAnimationActive: boolean }>).props.isAnimationActive).toBe(false)
  })

  it.each(["line", "area", "bar", "pie", "donut"])("uses the same non-animated policy for compact %s charts", (chartType) => {
    const rendered = buildRechartsTree(compileCompactGraphConfig({ chart_type: chartType, y_key: "count" }), [{ count: 4 }])
    const series = React.Children.toArray((rendered.props as { children: React.ReactNode }).children)
      .filter((child): child is React.ReactElement<{ isAnimationActive?: boolean }> => (
        React.isValidElement(child) && "isAnimationActive" in (child.props as object)
      ))
    expect(series).toHaveLength(1)
    expect(series[0].props.isAnimationActive).toBe(false)
  })

  it.each(["Line", "Area", "Bar", "Pie", "Scatter"])("does not let raw %s configuration re-enable series animation", (seriesType) => {
    expect(() => buildRechartsTree({
      type: "ComposedChart",
      children: [{ type: seriesType, props: { isAnimationActive: true } }],
    }, [])).toThrow(`Recharts ${seriesType} prop "isAnimationActive" is not supported`)
  })

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

describe("horizontal bar category axis", () => {
  function yAxisProps(rows: Array<Record<string, unknown>>) {
    const rendered = buildRechartsTree(compileCompactGraphConfig({
      chart_type: "bar",
      orientation: "horizontal",
      x_key: "name",
      y_key: "count",
    }), rows)
    const axis = React.Children.toArray((rendered.props as { children: React.ReactNode }).children)
      .find((child) => React.isValidElement(child) && (child.props as { type?: string }).type === "category")
    return (axis as React.ReactElement<{ width: number }>).props
  }

  it("sizes the axis from the longest label", () => {
    const short = yAxisProps([{ name: "A", count: 1 }]).width
    const longer = yAxisProps([{ name: "Clinic Alpha", count: 1 }]).width
    expect(longer).toBeGreaterThan(short)
  })

  it("caps the axis width for very long labels", () => {
    expect(yAxisProps([{ name: "x".repeat(200), count: 1 }]).width).toBe(CATEGORY_AXIS_MAX_WIDTH)
  })

  it("truncates with an ellipsis only beyond the cap", () => {
    const long = "x".repeat(80)
    expect(truncateCategoryLabel("Clinic", categoryAxisWidth(["Clinic"]))).toBe("Clinic")
    const truncated = truncateCategoryLabel(long, CATEGORY_AXIS_MAX_WIDTH)
    expect(truncated.endsWith("\u2026")).toBe(true)
    expect(truncated.length).toBeLessThan(long.length)
  })

  it("applies an author tickFormatter to category labels", () => {
    const rendered = buildRechartsTree({
      type: "BarChart",
      props: { layout: "vertical" },
      children: [{ type: "YAxis", props: { type: "category", dataKey: "name", tickFormatter: (v: unknown) => `<${String(v)}>` } }],
    }, [{ name: "Clinic" }])
    const axis = React.Children.toArray((rendered.props as { children: React.ReactNode }).children)[0] as React.ReactElement<{
      tick: (props: { payload: { value: string } }) => React.ReactElement<{ children: React.ReactNode[] }>
    }>
    const tick = axis.props.tick({ payload: { value: "Clinic" } })
    expect(tick.props.children).toContain("<Clinic>")
  })
})

describe("dimension series rendering", () => {
  it.each([["bar", true, "Bar"], ["bar", false, "Bar"], ["area", true, "Area"], ["line", false, "Line"]])("prepares %s with stacked=%s", (chartType, stacked, kind) => {
    const { rows, tree } = prepareCompactGraph({
      chart_type: String(chartType), stacked: Boolean(stacked),
      x_key: "week", y_key: "visits_count", series_by: "segment",
    }, [{ week: "May 26", segment: "Top user", visits_count: "3" }, { week: "May 26", segment: "Everyone else", visits_count: 7 }])
    expect(rows).toHaveLength(1)
    const series = tree.children?.filter((node) => node.type === kind) ?? []
    expect(series.map((s) => s.props?.name)).toEqual(["Top user", "Everyone else"])
    expect(series.map((s) => s.props?.stackId)).toEqual(stacked ? ["stack", "stack"] : [undefined, undefined])
    expect(new Set(series.map((s) => s.props?.fill ?? s.props?.stroke)).size).toBe(2)
    expect(tree.children?.some((node) => node.type === "Legend")).toBe(true)
    expect(tree.children?.find((node) => node.type === "YAxis")?.props?.allowDecimals).toBe(false)
    expect(() => buildRechartsTree(tree, rows)).not.toThrow()
  })

  it("rejects string series with corrective guidance", () => {
    expect(() => prepareCompactGraph({ series: "segment", y_key: "count" }, [])).toThrow("series_by")
  })

  it.each([{ series_by: null }, { series: ["count"] }, { chart_type: "pie" }, { y_key: undefined }])("rejects incomplete or ambiguous dimension config", (changes) => {
    const base = { chart_type: "bar", x_key: "week", y_key: "count", series_by: "segment" }
    expect(() => prepareCompactGraph({ ...base, ...changes }, [])).toThrow()
  })

  it("retains inferred count-axis semantics for dimension series", () => {
    const { tree } = prepareCompactGraph({ chart_type: "bar", x_key: "date", y_key: "visits_count", series_by: "segment" }, [{ date: "a", segment: "A", visits_count: 2 }])
    expect(tree.children?.find((node) => node.type === "YAxis")?.props).toMatchObject({ allowDecimals: false, domain: [0, "dataMax"] })
  })

  it("preserves the unambiguous empty-array fallback for existing wide-format artifacts", () => {
    const tree = compileCompactGraphConfig({ chart_type: "bar", series: [], y_key: "count", data_label: "Visits" })
    expect(tree.children?.find((node) => node.type === "Bar")?.props).toMatchObject({ dataKey: "count", name: "Visits" })
  })

  it("preserves legacy fallback labels while rendering valid measure keys", () => {
    const tree = compileCompactGraphConfig({ series: [{ data_key: "count", label: 42 }] })
    expect(tree.children?.find((node) => node.type === "Line")?.props).toMatchObject({ dataKey: "count", name: "count" })
  })

  it("preserves the omitted-series fallback when a stored artifact uses null", () => {
    const tree = compileCompactGraphConfig({ chart_type: "bar", series: null, y_key: "count", data_label: "Visits" })
    expect(tree.children?.find((node) => node.type === "Bar")?.props).toMatchObject({ dataKey: "count", name: "Visits" })
  })

  it.each([
    ["count", 42],
    [{ data_key: "count" }, {}],
    [{ data_key: 42, y_key: "count" }],
  ])("preserves valid legacy series entries and alias fallbacks (%j)", (...series) => {
    const tree = compileCompactGraphConfig({ chart_type: "bar", series })
    expect(tree.children?.filter((node) => node.type === "Bar").map((node) => node.props?.dataKey)).toEqual(["count"])
  })

  it.each([[42, {}], ["", "  "]])("preserves numeric-column inference for legacy arrays with no usable series (%j)", (...series) => {
    expect(normalizeGraphSeries(series, "count")).toEqual([])
  })

})
