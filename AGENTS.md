# Claude Code instructions

## Language

- Write all code, comments, and documentation in English.

## Wording

- Write all prose in ASD-STE100 Simplified Technical English: comments,
  docs, the text that the commands show, test names, and commit messages.
- Use approved words with one meaning each ("make sure", not "ensure";
  "start", not "begin"; "show", not "display"). Technical names and
  verbs (analyse, encode, download) are allowed.
- Keep sentences short: 20 words for instructions, 25 for descriptions.
- Use simple tenses and the active voice. No -ing verb forms, no perfect
  tenses, no contractions, no idioms, no semicolons between clauses.
- Write instructions in the imperative, one per sentence, with the
  condition first ("If X, do Y").
- Do not change text that dj-librarian writes into the library and finds
  again later (cue names, the comment prefix, playlist names) for
  wording. A changed text breaks the libraries that have the old one.

## Comments

- Only comment the non-obvious: a hidden constraint, a workaround for a
  specific bug, or an invariant that would surprise a reader.
- Do not describe what the code does — well-named identifiers already do that.
- Keep comments short: one line is almost always enough.

## Git

- Never add a `Co-Authored-By` trailer to commits.
- Commit messages: short imperative title, then a one-paragraph body
  explaining the why. Do not describe what the code does — the diff
  shows that.
- Address review feedback by amending the relevant commit rather than
  adding a separate "address review" commit, so the branch keeps a
  clean, logical history. Only add a new commit when amending is not
  possible (e.g. the commit is already merged).

## Pull requests

- When a PR includes visual changes, always attach before/after
  screenshots in the description.
