"""The frontmatter template create_note writes: what fields exist, and their
allowed values. Kept separate from server.py so the shape of a note's
frontmatter is one importable, reviewable thing rather than scattered through
the tool's logic.

Notes are append-only: this template is only ever applied at creation, never
to rewrite an existing note. VERSION is stamped into every note create_note
writes, recording which version of this template it was created under.

Bump VERSION (semver) whenever this file's shape changes - a field added,
renamed, or reordered in server.py's frontmatter construction, or a change to
TYPE_FOLDER/VALID_AUTHORS/TYPE_FIELD below. Never bump it retroactively: an
existing note's stamped version must keep describing the template that was
actually current when that note was written, so a future reader can branch on
`version` to handle older structures correctly instead of assuming today's
shape applies everywhere.
"""

VERSION = "0.4.0"

# The authoritative definition of what each VERSION means: the exact field
# list, order, and semantics that version's frontmatter has. A note's
# `version` field is this table's key - anything reading notes back (rather
# than only ever writing new ones, which is all this server does today)
# looks its note's version up here to know how to parse it, rather than
# assuming the current shape applies to every note in the vault.
#
# 0.4.0 (current): the same fields as 0.3.0. A type no longer fixes a note's folder: TYPE_FOLDER
#   is only where a note goes when no folder is given (every type but directives now defaults to
#   nuru/), and a new note may be created in any folder under nuru/ or shared/ (NEW_NOTE_ROOTS).
#
# 0.3.0: the same fields as 0.2.0. A daily note now lives in nuru/: it is Nuru's own
#   daily (and its reminders), any author may make one, and the user's dailies stay in user/,
#   which Nuru's server never writes.
#
# 0.2.0: the same fields as 0.1.0. What changed is where one type
#   lives: a correction is no longer only the user's - it lives in nuru/, any
#   author may make one, and create_note opens its body with a line linking
#   every note it corrects ("Corrects: [[<id>]], ..."), so the corrected
#   notes' backlinks show it.
#
# 0.1.0:
#   Field order: id, version, type, created, author, supersedes,
#   [<type-specific field>], tags, aliases.
#     - id: ULID, minted once by create_note. Immutable - write_note never
#       touches frontmatter.
#     - version: this table's key.
#     - type: one of TYPE_FOLDER's keys; fixes the note's folder.
#     - created: "YYYY-MM-DDTHH:mm", server clock, set once at creation.
#     - author: one of VALID_AUTHORS.
#     - supersedes: id of the note this one replaces, or blank.
#     - <type-specific field>: present only for a type in TYPE_FIELD, named
#       and validated per TYPE_FIELD (date: "YYYY-MM-DD"; session:
#       "YYYY-MM-DD-NN"; targets: a list of existing note ids).
#     - tags: block list, [type, *any extra caller-supplied tags]. No tag
#       contains whitespace.
#     - aliases: block list, [title, id].



# Frontmatter fields the server owns are minted here, never accepted from the
# caller. Folder is keyed by type; the eleven types are exactly these keys.
TYPE_FOLDER = {
    "directives": "user",
    "daily": "nuru",
    "correction": "nuru",
    "profile": "nuru",
    "preferences": "nuru",
    "person": "nuru",
    "topic": "nuru",
    "index": "nuru",
    "transcript": "nuru",
    "project": "nuru",
    "session": "nuru",
}

VALID_AUTHORS = {"user", "nuru", "both"}

# Extra frontmatter field each type requires, beyond the universal ones.
# type_fields passed to create_note must match exactly - no more, no less.
TYPE_FIELD = {
    "daily": "date",
    "correction": "targets",
    "transcript": "session",
    "session": "session",
}

# Where a new note may be created: any folder under these. (directives stay in user/, which
# Nuru's server never writes.)
NEW_NOTE_ROOTS = ("nuru", "shared")

# The type a new note gets when none is given.
DEFAULT_TYPE = "topic"

# One line on what each type is for: the model's write_note description and Laya's note_type
# question both read these, so the two can't drift. daily and directives are left out - daily is
# asked for by name, and directives are the user's.
TYPE_DESCRIPTIONS = {
    "topic": "a subject being learned, tracked or thought about",
    "person": "about one person: who they are, their role, how to reach them, what they are like",
    "project": "work with a goal and tasks, often shared",
    "session": "a record of one working session: what was done and decided",
    "transcript": "the record of a conversation or meeting, close to what was said",
    "index": "a list of links to other notes on one theme",
    "profile": "who the user is: background, skills, history",
    "preferences": "how the user likes things done",
    "correction": "says another note is wrong and gives the right information",
}
