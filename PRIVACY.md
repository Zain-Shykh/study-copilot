
# Local Agent — Privacy Notice

*Effective 14 September 2026*

**In plain terms:** Local Agent is a personal WhatsApp assistant built and
run by its one owner, for that owner's own Gmail and Google Classroom
account. It has no other users, shows no ads, and never sells or shares
data with advertisers or data brokers.

## 1. Who this covers
Local Agent is a single-user personal tool, not a public product. It is
operated by its owner for that owner's own accounts only, and is not
distributed or made available to any other person.

## 2. What data is accessed
With the owner's Google account authorization:
- `gmail.readonly` — inbox messages
- `classroom.courses.readonly`
- `classroom.coursework.me.readonly`
- `classroom.announcements.readonly`
- `drive.readonly` — assignment attachments

Separately, it reads the text of WhatsApp messages sent by the owner to the
tool's WhatsApp Business number, via the Meta Cloud API, to respond to them.

## 3. How that data is used
- Answering the owner's own questions about their inbox and coursework, on request
- Sending a batched digest of new mail and new/due Classroom items, twice daily
- Drafting a homework response from an assignment's own materials — shown to the owner for review before anything is finalized
- Sending an email or submitting an assignment **only** after the owner explicitly approves that specific action

## 4. Where data is processed and stored
State, session tokens, and checkpoints are stored in a PostgreSQL database
on the owner's own machine — not a shared or third-party hosted database.
These external services process data to carry out the actions above, each
under their own standard API terms:

| Service | Data | Purpose |
|---|---|---|
| Google APIs | Gmail, Classroom, Drive content | Fetch the data in §2 |
| Google Gemini API | Email/Classroom content | Generate summaries and digests |
| Anthropic Claude | Assignment materials | Research and draft responses (owner's own subscription) |
| Meta WhatsApp Cloud API | Message text | Send/receive WhatsApp messages |

## 5. What is never done with this data
Never sold or rented; never used for advertising or analytics products;
never shared beyond the processors in §4; never used to train a model
beyond what those processors' own API terms already permit.

## 6. Retention and revocation
Retained only as long as the tool runs, only for the purposes in §3.
- Google access — revoke anytime at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
- WhatsApp — stop messaging the tool's number

## 7. Contact
helloaidummy@gmail.com
