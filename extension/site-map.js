// Shared site classification used by the content script, background worker,
// and popup. Kept mostly as plain data (no logic) so it's easy to audit/extend.
//
// "ai_assistant" -> sites where the user is generating output WITH an AI, or
//                    pulling ready-made answers from a homework-answer site
//                    (prompt drafting, chat conversation, code copilot chat,
//                    Chegg/CourseHero-style answer lookup).
// "assessment"   -> sites where the user is taking a graded quiz/exam or
//                    submitting a graded assignment. STRICT mode: any paste,
//                    any AI-correlated paste, and tab-switching away during
//                    an active session are all weighted much harder here
//                    than on a normal writing surface (see scoring.py).
// "writing"      -> sites where the user does independent creative/technical
//                    work (docs, notes, code editors, essays) with no
//                    specific grading context detected.
// Anything not matched falls back to a lightweight generic detector in
// content-script.js that only activates if the page has a substantial
// textarea/contenteditable, and additionally checks the page title for
// exam/quiz/assignment keywords to catch assessment platforms not in the
// curated lists below (e.g. self-hosted Moodle instances, campus-specific
// LMS domains).

const AUTONOMIZE_AI_ASSISTANT_HOSTS = [
  // General-purpose AI chat
  "chatgpt.com",
  "chat.openai.com",
  "claude.ai",
  "gemini.google.com",
  "bard.google.com",
  "copilot.microsoft.com",
  "www.bing.com",       // Bing Chat / Copilot surfaces
  "poe.com",
  "perplexity.ai",
  "www.perplexity.ai",
  "you.com",
  "character.ai",
  "huggingface.co",     // HF Chat / spaces running assistants
  // Homework-answer / solution-lookup sites — not "AI chat" in name, but
  // functionally the same "get the answer instead of doing the work"
  // pattern college students use, and correlate the same way with pastes.
  "www.chegg.com",
  "chegg.com",
  "www.coursehero.com",
  "coursehero.com",
  "www.brainly.com",
  "brainly.com",
  "www.mathway.com",
  "mathway.com",
  "www.symbolab.com",
  "symbolab.com",
  "www.quizlet.com",
  "quizlet.com",
  "www.slader.com",
  "slader.com"
];

const AUTONOMIZE_WRITING_HOSTS = [
  "docs.google.com",     // narrowed to non-/forms/ paths at runtime, see below
  "www.notion.so",
  "notion.so",
  "www.overleaf.com",
  "overleaf.com",
  "medium.com",
  "github.com",
  "gitlab.com",
  "leetcode.com",
  "www.hackerrank.com",
  "codeforces.com",
  "replit.com",
  "stackoverflow.com",
  "www.canva.com"
];

// Hosts that are ALWAYS an assessment/assignment context, regardless of path.
const AUTONOMIZE_ASSESSMENT_HOSTS = [
  "classroom.google.com",
  "turnitin.com",
  "www.turnitin.com",
  "gradescope.com",
  "www.gradescope.com",
  "proctoru.com",
  "www.proctoru.com",
  "examity.com",
  "www.examity.com",
  "honorlock.com",
  "www.honorlock.com",
  "respondus.com",
  "www.respondus.com"
];

// Host suffixes (institution-branded subdomains) that are always assessment.
const AUTONOMIZE_ASSESSMENT_HOST_SUFFIXES = [
  ".blackboard.com",
  ".instructure.com",   // Canvas
  ".brightspace.com",   // D2L
  ".proctoru.com"
];

// hostname+path rules for platforms that host both regular and graded
// content on the same domain (so a bare hostname match would over-fire).
const AUTONOMIZE_ASSESSMENT_PATH_RULES = [
  // Google Forms — the single biggest way college quizzes get built.
  (host, path) => host === "docs.google.com" && path.startsWith("/forms/"),
  // Canvas quiz/assignment routes (in addition to the instructure.com suffix
  // rule above, kept here so a bare "instructure.com" without subdomain still matches).
  (host, path) => host.endsWith("instructure.com") && (path.includes("/quizzes/") || path.includes("/assignments/")),
  // Moodle's URL structure is standardized across institutions even though
  // the domain itself is self-hosted and unpredictable.
  (host, path) => path.includes("/mod/quiz/") || path.includes("/mod/assign/"),
];

// Title/URL keywords used by the generic fallback detector (content-script.js)
// to catch assessment platforms not covered by the lists/rules above.
const AUTONOMIZE_ASSESSMENT_KEYWORDS = [
  "quiz", "exam", "midterm", "final exam", "assessment", "test attempt",
  "proctor", "assignment submission", "graded quiz"
];

// Minimum visible characters in a single editable surface before the
// generic detector treats an unlisted site as a "writing" context.
const AUTONOMIZE_GENERIC_MIN_CHARS = 40;

function autonomizeClassify(hostname, pathname) {
  if (AUTONOMIZE_AI_ASSISTANT_HOSTS.includes(hostname)) return "ai_assistant";

  if (AUTONOMIZE_ASSESSMENT_HOSTS.includes(hostname)) return "assessment";
  if (AUTONOMIZE_ASSESSMENT_HOST_SUFFIXES.some((suf) => hostname.endsWith(suf))) return "assessment";
  if (AUTONOMIZE_ASSESSMENT_PATH_RULES.some((rule) => rule(hostname, pathname || "/"))) return "assessment";

  if (AUTONOMIZE_WRITING_HOSTS.includes(hostname)) return "writing";

  return "unknown"; // resolved at runtime by the generic detector
}

// Exposed for content-script.js (classic script, shares global scope).
