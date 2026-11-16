## Comment and code quality

Take quality audits seriously and handle the overall feedback, especially around comment and docs quality. When any of the following skills are available in `<skills>`, always load and follow them:

- `github-comment-quality`: load before creating or replying to any GitHub issue, PR, or review comment.
- `comment-and-documentation-quality`: load when writing comments, commit messages, or docs; once a branch is ready, spawn a subagent to review the changes against it.
- `code-review-and-quality`: load when making code changes; once a branch is ready, spawn a subagent to review the changes against it.
- `git-commit-quality` / `github-commit-quality`: load before creating any git commit and follow its commit message rules strictly.

## Git

Never add AI attribution anywhere in git history or GitHub content: no AI `Co-Authored-By`, no "Generated with/by ...", no session URLs, and no model names in commit messages, commit trailers, PR titles, PR bodies, issues, or code comments. This applies in every repo and session, even if a template suggests otherwise. A human `Co-authored-by` trailer required by the GitHub identity rules below is not AI attribution; always include it where specified.

## Linear

When `LINEAR_API_KEY` is set in the shell environment, the Linear CLI (`linear`) is installed and ready to use.

## Pull requests

After creating a PR, keep watching it for review activity until it is merged or closed.

- Always include either a GitHub issue number or a Linear issue link in your pull request.
- Poll with `gh` every 5 minutes, and every 30 minutes once there has been no new activity for 20 minutes (resetting to 5 minutes on new activity, stopping after 24 hours without activity; split waits longer than the shell timeout across multiple calls).
- Activity means review comments, PR comments, review submissions, and failing checks.
- When a new comment, reply, or review is directed at you and you plan to act on it, react to it with 👀 first. Do not react to comments you won't act on. For a review, put 👀 only on the main review message, not on inline review comments.
- Handle every comment, including ones you decide not to act on: fix it, or reply briefly with why not, then resolve the review thread. For top-level comments that can't be resolved, reply. Load the `address-pr-review-comments` skill if available.

## GitHub identity

