import type { Meta, StoryObj } from "@storybook/react-vite"

const colorTokens = [
  "background",
  "foreground",
  "card",
  "popover",
  "primary",
  "secondary",
  "muted",
  "accent",
  "destructive",
  "border",
  "input",
  "ring",
  "chart-1",
  "chart-2",
  "chart-3",
  "chart-4",
  "chart-5",
  "sidebar",
]

const radii = ["sm", "md", "lg", "xl", "2xl"]

const meta = {
  title: "Design Primitives/Tokens",
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

function Swatch({ token }: { token: string }) {
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div
        className="h-16 border-b"
        style={{ backgroundColor: `var(--${token})` }}
        aria-label={token}
      />
      <div className="space-y-1 p-3">
        <div className="text-sm font-medium">{token}</div>
        <div className="font-mono text-xs text-muted-foreground">{`var(--${token})`}</div>
      </div>
    </div>
  )
}

function TokenPanel({ mode }: { mode: "light" | "dark" }) {
  return (
    <section className={mode === "dark" ? "dark" : undefined}>
      <div className="rounded-xl border bg-background p-6 text-foreground">
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold capitalize">{mode} theme</h2>
          <span className="text-sm text-muted-foreground">Tailwind CSS variables</span>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {colorTokens.map((token) => (
            <Swatch key={`${mode}-${token}`} token={token} />
          ))}
        </div>
      </div>
    </section>
  )
}

export const Colors: Story = {
  render: () => (
    <main className="grid gap-8 px-8 py-10">
      <TokenPanel mode="light" />
      <TokenPanel mode="dark" />
    </main>
  ),
}

export const RadiusScale: Story = {
  render: () => (
    <main className="px-8 py-10">
      <div className="grid max-w-3xl gap-4 sm:grid-cols-5">
        {radii.map((radius) => (
          <div key={radius} className="space-y-3">
            <div
              className="h-24 border bg-card shadow-sm"
              style={{ borderRadius: `var(--radius-${radius})` }}
            />
            <div>
              <div className="text-sm font-medium">{radius}</div>
              <div className="font-mono text-xs text-muted-foreground">
                {`var(--radius-${radius})`}
              </div>
            </div>
          </div>
        ))}
      </div>
    </main>
  ),
}
