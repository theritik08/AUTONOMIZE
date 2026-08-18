#!/usr/bin/env python3
r"""Seed a small demo question bank.

    python3 seed_question_bank.py

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------

It is a handful of questions so the retrieval layer can be demonstrated
end to end without an institution having authored a bank first.

It is NOT a curriculum, and the questions here are not calibrated against
anything. In a real deployment the bank is authored by the faculty who set
the work — they are the only people who know which concepts an assignment
was meant to teach, and the difficulty values below are placeholders that
only real response data can replace.

That distinction is worth being loud about, because a demo bank quietly
becoming "the Autonomize question set" would be the project claiming
educational authority it has not earned.

The concepts chosen are deliberately generic study-skill and introductory
computing topics — the kind any pilot could replace wholesale without
touching code.
"""
import sys

from _env import load_dotenv

load_dotenv()

import db  # noqa: E402
import retrieval  # noqa: E402

CONCEPTS = [
    ("big-o", "Time complexity and Big-O", "Computer Science"),
    ("recursion", "Recursion and base cases", "Computer Science"),
    ("normalisation", "Database normalisation", "Computer Science"),
    ("citation", "Citing sources correctly", "Academic Writing"),
    ("thesis", "Thesis statements and argument structure", "Academic Writing"),
]

QUESTIONS = [
    # concept, prompt, options, answer index, difficulty
    ("big-o", "A loop that halves the remaining input each step runs in:",
     ["O(n)", "O(log n)", "O(n log n)", "O(n squared)"], 1, 0.4),
    ("big-o", "Two sequential loops over the same list of n items give:",
     ["O(n squared)", "O(2n), which simplifies to O(n)", "O(log n)", "O(1)"], 1, 0.35),
    ("big-o", "Big-O describes:",
     ["exact running time in seconds",
      "how running time grows as input grows",
      "how much memory a language uses",
      "the number of lines of code"], 1, 0.25),
    ("big-o", "A nested loop over the same n-item list is usually:",
     ["O(n)", "O(log n)", "O(n squared)", "O(1)"], 2, 0.3),

    ("recursion", "A recursive function without a base case will:",
     ["return null", "run until the stack overflows",
      "silently return 0", "be optimised away"], 1, 0.3),
    ("recursion", "The base case of a recursive function is:",
     ["the first call made",
      "the condition under which it stops recursing",
      "the largest input it accepts",
      "the return type"], 1, 0.25),
    ("recursion", "Tail recursion is notable because:",
     ["it always runs faster",
      "the recursive call is the last operation, so some compilers reuse the frame",
      "it needs no base case",
      "it cannot overflow in any language"], 1, 0.55),

    ("normalisation", "First normal form requires that:",
     ["every table has a foreign key",
      "each column holds a single, indivisible value",
      "the table has fewer than ten columns",
      "all values are unique"], 1, 0.4),
    ("normalisation", "Normalisation primarily reduces:",
     ["query time", "disk cost", "data redundancy and update anomalies",
      "the number of tables"], 2, 0.35),
    ("normalisation", "Denormalisation is sometimes chosen to:",
     ["save storage", "speed up reads at the cost of redundancy",
      "enforce referential integrity", "satisfy third normal form"], 1, 0.5),

    ("citation", "You paraphrase an author's idea in your own words. You:",
     ["need no citation because the words are yours",
      "still cite it, because the idea is theirs",
      "cite only if it is a direct quote",
      "add it to a bibliography but not the text"], 1, 0.3),
    ("citation", "A citation exists mainly to:",
     ["make the work longer",
      "let a reader find and check the source",
      "satisfy a word count",
      "show how many books you read"], 1, 0.2),
    ("citation", "Common knowledge, such as a well-known historical date:",
     ["always needs a citation",
      "generally does not need one",
      "needs three separate citations",
      "can never be included"], 1, 0.4),

    ("thesis", "A strong thesis statement is:",
     ["a description of the topic",
      "an arguable claim the essay then supports",
      "a question left open for the reader",
      "a summary of the sources"], 1, 0.35),
    ("thesis", "Which is NOT a thesis statement?",
     ["Remote work reduces junior developers' informal learning.",
      "This essay discusses remote work.",
      "Remote work improves retention more than pay rises do.",
      "Remote work harms teams that rely on tacit knowledge."], 1, 0.4),
    ("thesis", "Evidence in an argumentative essay should:",
     ["appear only in the conclusion",
      "support the specific claim being made in that paragraph",
      "be as long as possible",
      "come from a single source"], 1, 0.3),
]


def main():
    db.init_db()
    added_concepts = added_questions = 0

    with db.get_conn() as conn:
        existing = {c["concept_id"] for c in retrieval.list_concepts(conn)}
        for concept_id, name, subject in CONCEPTS:
            if concept_id in existing:
                continue
            retrieval.add_concept(conn, concept_id, name, subject)
            added_concepts += 1

        for i, (concept_id, prompt, options, answer, difficulty) in enumerate(QUESTIONS):
            question_id = f"{concept_id}-{i:03d}"
            already = conn.execute(
                db.q("SELECT question_id FROM questions WHERE question_id = ?"),
                (question_id,),
            ).fetchone()
            if already:
                continue
            retrieval.add_question(conn, question_id, concept_id, prompt,
                                   options, answer, difficulty)
            added_questions += 1

    print(f"concepts added:  {added_concepts}")
    print(f"questions added: {added_questions}")
    print()
    print("This is a DEMO bank. In a real deployment the faculty who set the")
    print("work author the questions — they are the only people who know what")
    print("an assignment was meant to teach, and the difficulty values here")
    print("are placeholders that only real response data can replace.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
