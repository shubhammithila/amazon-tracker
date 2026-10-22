# Ads: the once-a-day limit per RULE, and a saved-rules panel that can edit

22 Oct 2026. Asked for as *"Multiple rules can be applied to a keyword but the same rule cannot be
applied to a keyword more than once in a day. Currently it is only one rule can be applied on a
keyword once a day"* and *"there is no option to edit the rule or delete the rule. Make it better."*

## The guard

- **A rule is identified by what it does**: its conditions plus action and amount, as the server's
  `_rule_summary` writes them (`Spend > 100, ROAS < 2 -> bid decrease 10%`). That sentence is already
  stored on every ledger row (`ads_mutation.rule_summary`), so **no migration**. Two saved rules with
  identical contents are the same rule; editing a number makes a different rule.
- **Preview**: a keyword is left unticked (`changed_today`) only when THIS rule's sentence is on an
  `applied` ledger row for it dated today (IST). A keyword changed today by a DIFFERENT rule stays
  ticked and carries `other_today` — `[{rule, at, what}]`, `what` = `bid ₹x → ₹y` or `turned OFF/ON` —
  shown on the row as a note. Pause/enable flip-flops are allowed and flagged the same way (owner's
  choice).
- **The true current bid is unchanged**: the newest applied bid from the ledger, whichever rule set it.
  So two different −10% rules on one day compound to −19%; that is now a choice, made visible by the note.
- **Apply re-checks** with the same rule sentence the preview returned, so an open preview cannot send
  one rule twice. A row whose same-rule change was UNDONE (`reverted`) is not counted.
- `plan_run` keeps its old behaviour when called without the per-rule data (any change today blocks),
  so nothing else changes meaning.

## The saved-rules panel

- A list of saved rules, one row each: name · the rule in words · last run · **Use · Edit · Copy ·
  Delete**. A search box appears above 8 rules.
- **Use** loads into the builder (as today). **Edit** loads it and shows "Editing: name" with a name box
  and **Save changes** (renaming allowed) · **Save as new** · **Cancel**. **Copy** loads it as
  "name (copy)" in new-rule mode. **Delete** asks "Delete name?" and removes it.
- New rule: a name box and **Save**; a name already used is refused with an offer to replace.
- The `window.prompt` and the lone × are gone.
- Server: `POST /ads/rules` takes an optional `id` (update that row, rename allowed, refused 409 if the
  name belongs to another rule) and an optional `replace` (overwrite a same-named rule; without it a
  taken name is a 409). `last_run_at` is stamped when a run is applied from a saved rule's sentence.

## Tests
Same rule blocked; different rule ticked with `other_today`; apply refuses a same-rule repeat and lets
a different rule through; an undone change does not block; rename by id; name clash 409; no
`window.prompt` in the template; the panel's buttons exist. Mutations added to the ads harness.
