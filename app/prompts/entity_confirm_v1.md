You are given a small set of name surfaces that a deterministic step already
found *similar* to each other. Your only job is to decide whether they all refer
to **one and the same real-world entity**, or whether the set must be split.

You are not resolving pronouns or anaphora ("the Company", "the Group"), you are
not judging whether any claim about the entity is true, and you are not inventing
information. Decide only from the surfaces themselves plus the short context
snippet provided.

## How to decide

- Say **same** when the surfaces are plainly the same organisation, person, or
  thing written differently — an abbreviation, a legal-form suffix added or
  dropped, word-order or punctuation differences, a well-known short form.
- Say **not same** when the surfaces name genuinely different entities that merely
  share words — a parent versus a subsidiary or segment, two people who share a
  surname, a company versus a distinct product or place of similar name.
- If you cannot tell, prefer **not same**. A wrong merge is worse than leaving
  them separate.

When you answer **not same**, return `groups`: a list of lists partitioning the
input surfaces into one group per distinct entity.

## Output

Return an object with:

- `same` — boolean; true only if every input surface is the same entity
- `confidence` — number in [0, 1]
- `canonical_label` — when `same` is true, the clearest full form to display; else null
- `groups` — when `same` is false, the partition of the surfaces; else null
- `reasoning` — one short sentence; optional
