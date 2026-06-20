import { describe, it, expect } from "vitest"
import { render, screen } from "@testing-library/react"
import { CrossOppMeasureOutput } from "./CrossOppMeasureOutput"

describe("CrossOppMeasureOutput", () => {
  it("renders committed lineage", () => {
    render(
      <CrossOppMeasureOutput
        workspaceId="w1"
        output={{
          status: "committed",
          measure: "birth_weight",
          lineage: [
            {
              opportunity_id: "10012",
              status: "resolved",
              confidence: 0.97,
              column: "child_weight_birth",
              matched_label: "Birth weight (g)",
              sql_expression: "CAST(child_weight_birth AS NUMERIC)",
            },
          ],
        }}
      />,
    )
    expect(screen.getByTestId("crossopp-measure-output-birth_weight")).toBeInTheDocument()
    expect(screen.getByText("child_weight_birth")).toBeInTheDocument()
  })

  it("renders approval controls when needs_approval", () => {
    render(
      <CrossOppMeasureOutput
        workspaceId="w1"
        output={{
          status: "needs_approval",
          draft_id: "d1",
          measure: "los",
          flagged: [
            {
              opp_id: "10013",
              guess: null,
              confidence: 0.2,
              shortlist: [{ column: "stay_len", label: "Stay (days)", type: "Int" }],
            },
          ],
          resolved: [],
        }}
      />,
    )
    expect(screen.getByTestId("crossopp-approval-d1")).toBeInTheDocument()
    expect(screen.getByTestId("crossopp-approve-reject-10013")).toBeInTheDocument()
  })

  it("renders proposed summary without crashing", () => {
    render(
      <CrossOppMeasureOutput
        workspaceId="w1"
        output={{
          status: "proposed",
          committed: ["birth_weight"],
          needs_approval: [{ measure: "los", draft_id: "d1", flagged: ["10013"] }],
        }}
      />,
    )
    expect(screen.getByTestId("crossopp-proposed")).toBeInTheDocument()
    expect(screen.getByText(/birth_weight/)).toBeInTheDocument()
    expect(screen.getByText(/los/)).toBeInTheDocument()
  })
})
