// frontend/src/components/ChatEmptyState/starterQuestions.ts

type Provider = "commcare" | "commcare_connect" | "ocs" | "default"

export const STARTER_QUESTIONS: Record<Provider, readonly string[]> = {
  commcare: [
    "How many cases were opened last month?",
    "Which mobile workers haven't submitted a form in the last week?",
    "Compare form submission volume this quarter vs last quarter.",
  ],
  commcare_connect: [
    "How many workers completed verified visits this week?",
    "Which opportunities have the highest payment volume this month?",
    "Compare average payment per worker across opportunities.",
  ],
  ocs: [
    "How many conversations did the bot handle in the last 7 days?",
    "What are the most common user messages this week?",
    "Compare session counts across bots this month vs last month.",
  ],
  default: [
    "What tables are available in my data?",
    "Show me a high-level overview of the schema.",
    "What are the most recently updated records?",
  ],
}

export function getStarterQuestions(provider: string | undefined): readonly string[] {
  if (!provider) return STARTER_QUESTIONS.default
  const key = provider as Provider
  return STARTER_QUESTIONS[key] ?? STARTER_QUESTIONS.default
}
