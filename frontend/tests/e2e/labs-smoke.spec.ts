/**
 * Labs smoke test — automated end-to-end flow.
 *
 * Uses persistent browser profile so OAuth session survives between runs.
 *
 * Usage:
 *   cd frontend && npx playwright test --project=labs-smoke --timeout 300000
 */
import { test, chromium } from "@playwright/test"

const LABS_URL = "https://labs.connect.dimagi.com/scout/"
const USER_DATA_DIR = "/tmp/scout-playwright-profile"

// Test account for Connect OAuth
const CONNECT_EMAIL = "jjackson+test@dimagi.com"
const CONNECT_PASSWORD = "Uganda123"

test("full labs flow: login → select workspace → chat", async () => {
  const browser = await chromium.launchPersistentContext(USER_DATA_DIR, {
    headless: false,
    viewport: { width: 1280, height: 800 },
  })
  const page = browser.pages()[0] || (await browser.newPage())

  try {
    // ── Step 1: Load the app ──
    console.log("\n── Step 1: Loading Scout...")
    await page.goto(LABS_URL, { waitUntil: "networkidle" })
    await page.screenshot({ path: "/tmp/labs-01-loaded.png" })
    console.log(`   URL: ${page.url()}`)

    // ── Step 2: Handle auth ──
    const pageText = await page.textContent("body")

    if (pageText?.includes("Sign in to your account")) {
      console.log("── Step 2: Login page — clicking CommCare Connect...")
      await page.screenshot({ path: "/tmp/labs-02-login.png" })

      const connectBtn = page.locator('[data-testid="oauth-login-commcare_connect"]')
      await connectBtn.click()
      await page.waitForTimeout(3000)
      await page.screenshot({ path: "/tmp/labs-03-after-click.png" })
      console.log(`   URL: ${page.url()}`)

      const url = page.url()
      if (url.includes("404") || (await page.textContent("body"))?.includes("Page not found")) {
        throw new Error(`OAuth 404! URL: ${url}`)
      }

      // Auto-fill Connect login if we're on the login page
      if (url.includes("connect.dimagi.com") && url.includes("login")) {
        console.log("   On Connect login page — filling credentials...")
        const emailInput = page.locator('input[placeholder="Enter Email ID"], input[type="email"], input[name="login"], input[name="username"]').first()
        const passwordInput = page.locator('input[type="password"]').first()

        await emailInput.waitFor({ timeout: 10_000 })
        await emailInput.fill(CONNECT_EMAIL)
        await passwordInput.fill(CONNECT_PASSWORD)
        await page.screenshot({ path: "/tmp/labs-03b-credentials.png" })

        // Click login button
        const loginBtn = page.locator('button:has-text("Login"), button:has-text("Sign in"), input[type="submit"]').first()
        await loginBtn.click()
        console.log("   Submitted login form...")

        await page.waitForTimeout(3000)
        await page.screenshot({ path: "/tmp/labs-03c-after-login.png" })
        console.log(`   URL: ${page.url()}`)
      }

      // Handle OAuth authorize page if it appears
      if (page.url().includes("/o/authorize")) {
        console.log("   On OAuth authorize page — clicking Allow...")
        const allowBtn = page.locator('button:has-text("Authorize"), input[value="Authorize"], button:has-text("Allow"), input[type="submit"][value="Authorize"]').first()
        if (await allowBtn.isVisible({ timeout: 5_000 }).catch(() => false)) {
          await allowBtn.click()
        }
        await page.waitForTimeout(3000)
      }

      // Wait for redirect back to Scout
      console.log("   Waiting for redirect back to Scout...")
      await page.waitForURL("**/scout/**", { timeout: 30_000 }).catch(async () => {
        console.log(`   Current URL: ${page.url()}`)
        await page.screenshot({ path: "/tmp/labs-03d-stuck.png" })
        throw new Error(`Did not redirect back to Scout. URL: ${page.url()}`)
      })

      await page.waitForTimeout(3000)
      await page.screenshot({ path: "/tmp/labs-04-post-auth.png" })
      console.log("   Auth complete!")
    } else if (pageText?.includes("failed to check auth")) {
      throw new Error("Auth check failed — API unreachable")
    } else {
      console.log("── Step 2: Already authenticated")
    }

    // ── Step 3: Check app state ──
    console.log("── Step 3: Checking app state...")
    await page.waitForTimeout(2000)
    await page.screenshot({ path: "/tmp/labs-05-app-state.png" })
    console.log(`   URL: ${page.url()}`)
    const bodyText = await page.textContent("body")
    console.log(`   Body preview: ${bodyText?.substring(0, 200)}`)

    // ── Step 4: Find chat input ──
    console.log("── Step 4: Looking for chat input...")

    let chatInput = page.locator('input[placeholder*="Ask about your data"], textarea, [contenteditable="true"], [role="textbox"]').first()

    await page.screenshot({ path: "/tmp/labs-06-chat.png" })

    if (!(await chatInput.isVisible({ timeout: 5_000 }).catch(() => false))) {
      await page.screenshot({ path: "/tmp/labs-06-no-chat.png" })
      throw new Error(`No chat input found. URL: ${page.url()}`)
    }

    // ── Step 5: Send message ──
    console.log("── Step 5: Sending test message...")
    await chatInput.click()
    await chatInput.fill("What tables are available in this database?")
    await page.screenshot({ path: "/tmp/labs-07-typed.png" })
    await chatInput.press("Enter")
    console.log("   Sent!")

    // ── Step 6: Wait for response ──
    console.log("── Step 6: Waiting for response...")
    await page.waitForTimeout(5000)
    await page.screenshot({ path: "/tmp/labs-08-waiting.png" })
    await page.waitForTimeout(15000)
    await page.screenshot({ path: "/tmp/labs-09-response.png" })
    console.log("   Done!")

    console.log("\n All steps passed!")
  } finally {
    await browser.close()
  }
})
