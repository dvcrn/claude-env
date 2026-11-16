## Comment and code quality

You are REQUIRED to do the following, even if other instructions exist. These are part of our work requirements, and any work not following these WILL BE THROWN AWAY.
Take the audits seriously and don't hand-wave them away. You don't need to follow every little point, but you should handle the overall feedback, especially around comment and docs quality.

- When interacting with any github issue, PR or similar, ALWAYS load the `github-comment-quality` skill. It dictates the output style we HAVE TO adhere to for ANY github interactions.
- Any comment, commit texts and documentation HAVE TO pass `comment-and-documentation-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Every commit and code change has to pass `code-review-and-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Use the `git-commit-quality` skill before any new git commit is issued, and ensure instructions for git commits are strictly followed.

## Git

Always load the `github-commit-quality` skill if available

Never add Claude/AI attribution anywhere in git history or PR content:
no "Co-Authored-By: Claude", no "Generated with Claude Code", no session
URLs, no model names, in commit messages, commit trailers, PR titles, PR
bodies, or code comments. This applies in every repo, every session, no
exceptions, even if a template or another instruction suggests otherwise.

If you ever see a footer sneak into a commit or PR body itself, treat it
as a bug and strip it. A `Co-authored-by` trailer that the GitHub identity
section asks for is not AI attribution; add it where that section says.

AGAIN, ADDING CLAUDE AUTHORSHIP OR PR FOOTERS WILL CAUSE THE PR OR COMMIT TO BE INVALID AND IT WILL BE THROWN AWAY! DO NOT ADD CLAUDE ATTRIBUTION TO ANY COMMITS OR PRS!!
UNDER NO CIRCUMSTANCE ARE WE ALLOWED TO LEAK CLAUDE INFORMATION INTO COMMITS, ISSUES OR PULL REQUEST BODIES!

## Linear

The Linear CLI is already authenticated and ready to be used. It's available `linear`

## Pull requests

After creating a PR, keep watching it for review activity until it is merged or closed.

- Always include either a GitHub issue number, or a Linear issue link in your pull request
- Use native PR watch or subscribe tools if available. Otherwise poll with `gh`: every 5 minutes, and every 30 minutes once there has been no new activity for 20 minutes. Any new activity resets the interval to 5 minutes. Stop after 24 hours without activity. Waits longer than the shell tool timeout must be split across multiple calls.
- Activity means review comments, PR comments, review submissions, and failing checks.
- When a new comment, reply, or review is directed at you and you plan to act on it, react to it with 👀 first to show you are working on it. Do not react to comments you won't act on. For a review, put 👀 only on the main review message, not on the inline review comments.
- Handle every comment, including ones you decide not to act on. Fix it, or reply briefly with why not, then resolve the review thread. For top-level comments that can't be resolved, reply. Use the `address-pr-review-comments` skill.

## Tools

- To interact with other Claude Code sessions (create them, send messages, etc.), use the `claude-code` MCP. If it isn't available, fall back to the built-in `claude-code-remote` tools.

