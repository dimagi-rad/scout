const assert = require("node:assert/strict");
const test = require("node:test");
const { prepareClaude, finishClaude } = require("./ocr-workflow.cjs");
const { encodeState, readState } = require("./ocr-state.cjs");
const HEAD = "a".repeat(40),
  BASE = "b".repeat(40),
  POLICY = "c".repeat(64);
function harness() {
  const h = {
    env: {
      PR_NUMBER: "42",
      REVIEW_HEAD: HEAD,
      REVIEW_BASE: BASE,
      POLICY,
      GITHUB_REPOSITORY: "owner/repo",
      GITHUB_SERVER_URL: "https://github.com",
      GITHUB_RUN_ATTEMPT: "1",
      RUNNER_TEMP: "/tmp",
      CLAUDE_OUTCOME: "success",
      CLAUDE_CONCLUSION: "success",
      CLAUDE_RESULT: JSON.stringify({
        complete: true,
        reviewed_head: HEAD,
        blocking_findings: 0,
      }),
      EXECUTION_FILE: "/sdk.json",
    },
    context: { repo: { owner: "owner", repo: "repo" }, runId: 123 },
    outputs: {},
    files: {
      "/sdk.json": JSON.stringify([
        {
          type: "result",
          subtype: "success",
          is_error: false,
          permission_denials: [],
        },
      ]),
    },
    writes: [],
    failures: [],
    pr: { state: "open", head: { sha: HEAD }, base: { sha: BASE } },
  };
  h.comments = [
    {
      id: 1,
      user: { login: "github-actions[bot]", type: "Bot" },
      body:
        "<!-- scout-ocr-gate -->\nOriginal OCR explanation\n" +
        encodeState({
          version: 1,
          head: HEAD,
          base: BASE,
          policy: POLICY,
          run: "123",
          passed: true,
          claudeHead: null,
        }),
    },
  ];
  h.core = {
    setSecret: (value) => (h.secrets = (h.secrets || []).concat(value)),
    setOutput: (k, v) => (h.outputs[k] = v),
    info() {},
    warning: (w) => (h.warnings = (h.warnings || []).concat(w)),
    setFailed: (r) => h.failures.push(r),
    summary: {
      addRaw(s) {
        h.summary = (h.summary || "") + s;
        return this;
      },
      async write() {},
    },
  };
  h.fs = {
    writeFileSync: (p, s, options) => {
      h.writeOptions = { ...h.writeOptions, [p]: options };
      h.files[p] = s;
    },
    readFileSync: (p) => {
      if (!(p in h.files)) throw Error("PRIVATE PATH");
      return h.files[p];
    },
  };
  const publish = async (p) => {
    if (h.publishError) throw Error("PRIVATE MESSAGE");
    h.writes.push(p);
    const id = p.comment_id || 100 + h.comments.length;
    const value = {
      id,
      user: { login: "github-actions[bot]", type: "Bot" },
      body: p.body,
    };
    h.comments = h.comments.filter((c) => c.id !== id).concat(value);
    if (h.afterPublish) await h.afterPublish(p);
    return { data: value };
  };
  h.github = {
    rest: {
      issues: {
        listComments() {},
        createComment: publish,
        updateComment: publish,
      },
      pulls: {
        get: async () => ({ data: h.pr }),
        listReviewComments() {},
        listReviews() {},
      },
    },
    paginate: async (method) =>
      method === h.github.rest.issues.listComments ? h.comments : [],
  };
  return h;
}
async function prepared() {
  const h = harness();
  await prepareClaude(h);
  h.env.CLAUDE_RECEIPT = h.files["/tmp/scout-claude-receipt.json"];
  h.env.BASELINE_ISSUE_IDS = h.outputs.issue_ids;
  return h;
}
function reviewed(h) {
  h.comments.push({
    id: 500,
    user: { login: "github-actions[bot]", type: "Bot" },
    body:
      "No blocking findings after reviewing the exact range.\n<!-- scout-claude-artifact:v1 " +
      h.env.CLAUDE_RECEIPT +
      " -->",
  });
}
test("prepare emits unique run-bound receipt and pending status with fixed prefetch", async () => {
  const h = await prepared();
  const r = JSON.parse(h.env.CLAUDE_RECEIPT);
  assert.equal(r.run, "123");
  assert.equal(r.attempt, "1");
  assert.equal(r.head, HEAD);
  assert.match(r.nonce, /^[a-f0-9]{64}$/);
  assert.match(
    h.comments.find((c) => c.body.startsWith("<!-- scout-claude-review -->"))
      .body,
    /pending/,
  );
  const other = await prepared();
  assert.notEqual(other.env.CLAUDE_RECEIPT, h.env.CLAUDE_RECEIPT);
});
test("verified receipt is published before accepted Claude checkpoint advances", async () => {
  const h = await prepared();
  reviewed(h);
  h.writes = [];
  await finishClaude(h);
  assert.deepEqual(h.failures, []);
  assert.equal(readState(h.comments).claudeHead, HEAD);
  assert.match(h.writes[0].body, /Claude review: verified/);
  assert.match(h.writes.at(-1).body, /Original OCR explanation/);
  assert.equal(h.outputs.claude_verified, "true");
});
test("failed action, missing SDK, malformed output and absent artifact visibly block", async () => {
  for (const mutate of [
    (h) => (h.env.CLAUDE_OUTCOME = "failure"),
    (h) => delete h.files["/sdk.json"],
    (h) => (h.env.CLAUDE_RESULT = "PRIVATE malformed"),
    (h) => (h.comments = h.comments.filter((c) => c.id !== 500)),
  ]) {
    const h = await prepared();
    reviewed(h);
    mutate(h);
    await finishClaude(h);
    assert.ok(h.failures.length);
    assert.equal(readState(h.comments).claudeHead, null);
    assert.match(h.summary, /blocked/);
    assert.doesNotMatch(h.summary, /PRIVATE/);
    assert.notEqual(h.outputs.claude_verified, "true");
  }
});
test("positive blocking findings and changed base never advance checkpoint", async () => {
  for (const mutate of [
    (h) =>
      (h.env.CLAUDE_RESULT = JSON.stringify({
        complete: true,
        reviewed_head: HEAD,
        blocking_findings: 1,
      })),
    (h) => (h.pr.base.sha = HEAD),
  ]) {
    const h = await prepared();
    reviewed(h);
    mutate(h);
    await finishClaude(h);
    assert.equal(readState(h.comments).claudeHead, null);
    assert.ok(h.failures.length);
    assert.match(h.summary, /blocked/);
  }
});
test("publication failure cannot advance checkpoint or disclose exception content", async () => {
  const h = await prepared();
  reviewed(h);
  h.publishError = true;
  await finishClaude(h);
  assert.equal(readState(h.comments).claudeHead, null);
  assert.ok(h.failures.length);
  assert.doesNotMatch(h.failures.join(), /PRIVATE/);
  assert.notEqual(h.outputs.claude_verified, "true");
});
test("late older attempt never overwrites a newer pending receipt", async () => {
  const h = await prepared();
  reviewed(h);
  const newer = { ...h, env: { ...h.env, GITHUB_RUN_ATTEMPT: "2" } };
  await prepareClaude(newer);
  const pending = h.comments.find((c) =>
    c.body.startsWith("<!-- scout-claude-review -->"),
  ).body;
  h.writes = [];
  await finishClaude(h);
  assert.equal(h.writes.length, 0);
  assert.equal(
    h.comments.find((c) => c.body.startsWith("<!-- scout-claude-review -->"))
      .body,
    pending,
  );
  assert.equal(readState(h.comments).claudeHead, null);
});

test("preparation failures replace an earlier verified receipt with blocked", async () => {
  for (const stage of ["stale", "prefetch", "write"]) {
    const h = await prepared();
    reviewed(h);
    await finishClaude(h);
    h.env.GITHUB_RUN_ATTEMPT = "2";
    if (stage === "stale") h.pr.base.sha = HEAD;
    if (stage === "prefetch") {
      const prior = h.github.paginate;
      h.github.paginate = async (method) => {
        if (method === h.github.rest.pulls.listReviews)
          throw Error("PRIVATE API");
        return prior(method);
      };
    }
    if (stage === "write")
      h.fs.writeFileSync = () => {
        throw Error("PRIVATE DISK");
      };
    await assert.rejects(prepareClaude(h), /preparation failed/i);
    const receipt = h.comments.find((c) =>
      c.body.startsWith("<!-- scout-claude-review -->"),
    );
    assert.match(receipt.body, /blocked/);
    assert.doesNotMatch(receipt.body, /PRIVATE/);
  }
});
test("newer pending between verification publication and checkpoint write fences the older attempt", async () => {
  const h = await prepared();
  reviewed(h);
  h.afterPublish = async (payload) => {
    if (!payload.body.includes("Claude review: verified")) return;
    h.afterPublish = null;
    await prepareClaude({ ...h, env: { ...h.env, GITHUB_RUN_ATTEMPT: "2" } });
  };
  h.writes = [];
  await finishClaude(h);
  assert.equal(readState(h.comments).claudeHead, null);
  assert.equal(
    h.writes.filter((w) => w.body.startsWith("<!-- scout-ocr-gate -->")).length,
    0,
  );
  assert.match(
    h.comments.find((c) => c.body.startsWith("<!-- scout-claude-review -->"))
      .body,
    /pending/,
  );
  assert.notEqual(h.outputs.claude_verified, "true");
});

test("noncanonical OCR marker cannot silently claim checkpoint persistence", async () => {
  const h = await prepared();
  reviewed(h);
  const gate = h.comments.find((c) =>
    c.body.startsWith("<!-- scout-ocr-gate -->"),
  );
  gate.body = gate.body.replace('"version":1', '"version": 1');
  await finishClaude(h);
  assert.equal(readState(h.comments).claudeHead, null);
  assert.notEqual(h.outputs.claude_verified, "true");
  assert.ok(h.failures.length);
  assert.doesNotMatch(h.summary, /Claude review: verified/);
  assert.match(h.summary, /Claude review: blocked/);
});

test("every accepted OCR identity field fences Claude checkpoint publication", async () => {
  for (const patch of [
    { policy: "d".repeat(64) },
    { run: "122" },
    { passed: false },
    { head: "e".repeat(40) },
    { base: "f".repeat(40) },
  ]) {
    const h = await prepared();
    reviewed(h);
    const gate = h.comments.find((c) =>
      c.body.startsWith("<!-- scout-ocr-gate -->"),
    );
    gate.body =
      "<!-- scout-ocr-gate -->\n" +
      encodeState({ ...readState(h.comments), ...patch });
    await finishClaude(h);
    assert.equal(readState(h.comments).claudeHead, null);
    assert.notEqual(h.outputs.claude_verified, "true");
    assert.ok(h.failures.length);
  }
});

test("malformed or duplicate receipt state cannot be overwritten as trusted history", async () => {
  for (const corrupt of [
    (body) => body.replace('"head":"' + HEAD + '"', '"head":"wrong"'),
    (body) =>
      body + "\n" + body.slice(body.indexOf("<!-- scout-claude-state:")),
    (body) => body.replace('"status":"pending"', '"status":"invalid"'),
  ]) {
    const h = await prepared();
    const prior = h.comments.find((c) =>
      c.body.startsWith("<!-- scout-claude-review -->"),
    );
    prior.body = corrupt(prior.body);
    const body = prior.body;
    h.env.GITHUB_RUN_ATTEMPT = "2";
    await assert.rejects(prepareClaude(h), /preparation failed/i);
    assert.equal(h.comments.find((c) => c.id === prior.id).body, body);
  }
});

test("receipt nonce stays in private runner file rather than echoed step inputs", async () => {
  const h = await prepared();
  assert.equal(h.outputs.receipt, undefined);
  assert.deepEqual(h.secrets, [JSON.parse(h.env.CLAUDE_RECEIPT).nonce]);
  assert.equal(h.writeOptions["/tmp/scout-claude-receipt.json"].mode, 0o600);
  assert.equal(
    JSON.parse(h.files["/tmp/scout-claude-receipt.json"]).nonce,
    JSON.parse(h.env.CLAUDE_RECEIPT).nonce,
  );
  const workflow = require("node:fs").readFileSync(
    require("node:path").join(__dirname, "../workflows/ocr.yml"),
    "utf8",
  );
  assert.doesNotMatch(workflow, /claude_baseline.outputs.receipt/);
  assert.match(workflow, /scout-claude-receipt.json/);
});

test("missing or malformed private receipt blocks even with an otherwise valid artifact", async () => {
  for (const value of [undefined, "PRIVATE invalid json", "{}"]) {
    const h = await prepared();
    reviewed(h);
    if (value === undefined) delete h.files["/tmp/scout-claude-receipt.json"];
    else h.files["/tmp/scout-claude-receipt.json"] = value;
    await finishClaude(h);
    assert.equal(readState(h.comments).claudeHead, null);
    assert.notEqual(h.outputs.claude_verified, "true");
    assert.ok(h.failures.length);
    assert.doesNotMatch(h.summary, /PRIVATE/);
  }
});

test("checkpoint write must be observed before claiming successful persistence", async () => {
  const h = await prepared();
  reviewed(h);
  const original = h.comments.find((c) =>
    c.body.startsWith("<!-- scout-ocr-gate -->"),
  ).body;
  h.afterPublish = async (payload) => {
    if (payload.body.startsWith("<!-- scout-ocr-gate -->")) {
      h.comments.find((c) =>
        c.body.startsWith("<!-- scout-ocr-gate -->"),
      ).body = original;
    }
  };
  await finishClaude(h);
  assert.equal(readState(h.comments).claudeHead, null);
  assert.notEqual(h.outputs.claude_verified, "true");
  assert.ok(h.failures.length);
  assert.doesNotMatch(h.summary, /Claude review: verified/);
});

test("denied tool calls are logged to the run but kept out of PR comments", async () => {
  const h = await prepared();
  reviewed(h);
  h.files["/sdk.json"] = JSON.stringify([
    {
      type: "result",
      subtype: "success",
      is_error: false,
      permission_denials: [
        { tool_name: "Bash", tool_input: { command: "git grep PRIVATE | head" } },
      ],
    },
  ]);
  await finishClaude(h);
  assert.deepEqual(h.warnings, [
    'Denied tool call 1: Bash command="git grep PRIVATE | head"',
  ]);
  assert.ok(h.failures.length);
  assert.equal(readState(h.comments).claudeHead, null);
  for (const c of h.comments) assert.doesNotMatch(c.body, /PRIVATE/);
  assert.doesNotMatch(h.summary, /PRIVATE/);
});

test("Claude reviewer tools are an exact read-only allowlist", () => {
  const workflow = require("node:fs").readFileSync(
    require("node:path").join(__dirname, "../workflows/ocr.yml"),
    "utf8",
  );
  const lines = workflow.match(/--allowedTools "([^"]*)"/g);
  assert.equal(lines.length, 1);
  const tools = lines[0].slice('--allowedTools "'.length, -1).split(",");
  // git grep is excluded because -O/--open-files-in-pager runs an arbitrary
  // shell command; gh api can write with the job's PR/issue token.
  assert.deepEqual(tools, [
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git rev-parse:*)",
    "Bash(git merge-base:*)",
    "Bash(git ls-tree:*)",
    "Bash(git cat-file:*)",
    "Bash(git blame:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr comment:*)",
    "Read",
    "Grep",
    "Glob",
  ]);
  assert.match(workflow, /one command per Bash call, with no pipes, redirects/);
});
