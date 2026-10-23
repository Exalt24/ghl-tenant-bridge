# History import and the suppression list

Back to the [relay README](../README.md).

Two lists let the relay respect what happened before it was switched on, and what a person asked for.

- **Sent history:** contacts that were already sent a message kind before the relay existed. The relay does not send the same contact the same message kind again.
- **Suppression list:** contacts who must never be contacted (do-not-contact). The relay sends them nothing, whatever the message kind.

Both are filled from a CSV by `python -m relay.import_history`, and both are checked just before a delivery is sent.

## The command

```
python -m relay.import_history history.csv --kind ContactCreate            # dry run: a report, nothing written
python -m relay.import_history history.csv --kind ContactCreate --apply    # write it
python -m relay.import_history dnc.csv --list suppression --apply
```

It reads the same settings as the worker (`DATABASE_URL`, or `RELAY_DB_PATH` for SQLite, and the rest of the environment), so run it where the worker runs.

| flag | meaning |
|---|---|
| `--list history` (default) or `--list suppression` | which list the file feeds |
| `--kind NAME` | the message kind of every history row that has no `kind` cell. It is the HighLevel event type the relay stores, such as `ContactCreate`, and it is matched exactly |
| `--region US` | how a phone with no country is read. Default is `RELAY_DEFAULT_REGION`, else `US` |
| `--expect-rows N` | refuse the file unless it holds exactly N data rows (a cut short download is caught here) |
| `--apply` | write. Without it nothing is written |
| `--skip-invalid` | with `--apply`, write the valid rows even though some were refused |

### The file

A header row, then one contact per row. Column names are not case sensitive and unknown columns are ignored.

| column | needed | meaning |
|---|---|---|
| `contact` | always | a phone number or an email address |
| `kind` | history, unless `--kind` is given | the message kind. An empty cell falls back to `--kind` |
| `reason` | never | suppression only: why. An empty cell means `do-not-contact` |

The file is read as UTF-8 (with or without the mark Excel adds) or Windows-1252. UTF-16 is refused with a message that says to save it as CSV UTF-8.

### What it checks and says

- **Contacts are normalised.** A phone becomes E.164 and an email is lower cased, so `(555) 010-1234`, `555.010.1234`, `1 555 010 1234` and `+15550101234` are one contact. The rules are a small table in `relay/contacts.py` (US, CA, PH, GB, AU, NZ, IN, SG, IE); a number that carries its own country (`+63 917 123 4567`, `0063 917 123 4567`) is read the same under any default region.
- **Idempotent.** The key is the contact and the message kind for history, and the contact alone for suppression. A second import adds zero rows, and a repeat inside one file counts once.
- **All or nothing.** `--apply` writes in one transaction. A failure part way writes nothing.
- **A refused row is named, never dropped.** The report lists `line N: reason`. The reason never repeats the contact. Invalid rows make a dry run exit 1, and `--apply` then writes nothing at all unless `--skip-invalid` is passed, because a do-not-contact list with a quiet hole in it is worse than none. With `--skip-invalid` the rows are still listed and the exit code is still 1.
- **A short read is never "nothing to import".** A file with no data rows, a header without a `contact` column, a repeated column name, an unreadable file or a wrong `--expect-rows` is refused with exit 2 and nothing is written.

The report:

```
DRY RUN, nothing was written: history list
rows read: 1204
would insert: 1180
duplicates: 21
invalid: 3
suppressed: 17
  line 88: phone is not a valid US number: 7 digits
  line 301: email is not a valid address
  line 977: the row has 3 cells and the header has 2
```

`duplicates` counts repeats inside the file and rows the database already holds. `suppressed` (history only) counts history rows whose contact is on the suppression list; they are still imported, because they say what happened in the past.

Exit codes: 0 clean, 1 some rows were refused, 2 the file or the setup was refused.

## At send time

Just before a sink is called, the worker builds the keys of the lead (its email and its phone, normalised the same way) and looks them up.

| answer | what is stored on the delivery | the sink |
|---|---|---|
| a key is on the suppression list | state `skipped`, `effect_ref` `suppressed`, `last_error` `suppressed: <reason>`, and an attempts row with the same note | not called |
| a key has a history row of this lead's message kind | state `skipped`, `effect_ref` `already_sent`, `last_error` `already sent before the relay` | not called |
| neither | nothing changes | called as before |
| the lists cannot be read (database error) | the row stays pending and is retried | not called |

Suppression wins over history. The message kind of a lead is the `type` of its HighLevel event. A lead with no email and no phone (a Fathom meeting) is never blocked.

Find the refused deliveries with `SELECT * FROM deliveries WHERE effect_ref IN ('suppressed', 'already_sent')`. A refused delivery can be replayed with `force` after the contact is taken off the list (see the replay route in [routes.md](routes.md)); until then it stays refused.

## Limits

- The stored outcome is `skipped` with a distinct `effect_ref`, not a new `suppressed` state. The `state` column has a CHECK constraint that an existing database cannot change without rebuilding the table, and every reader of `state` already understands `skipped`.
- Both lists are global, not per tenant: a contact suppressed once is suppressed for every tenant of the relay.
- A lead whose phone and email are both unreadable has no key and cannot match anything. A lead known by a phone on the list and an email that is not is still refused (either key is enough).
- The relay does not add a contact to the history when it sends one. The history holds what was imported.
- A phone is read by the table in `relay/contacts.py`. A country that is not in the table is accepted only in international form (`+` and 8 to 15 digits), with no length check of its own.
