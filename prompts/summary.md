Generate meeting notes from this transcript. The reader is me — {user_role} — and I will circulate these notes to my team and use them to track action items. Write like I took the notes myself: clear, terse, factual. No narrative prose. No framing verbs ("discussed", "emphasized", "noted that"). State what was said.

Audience assumption: I know what {organization} is, what our services or offerings do, and who the {organization} employees in the meeting are. Skip that context.

Speaker labels in the transcript: [You] is me ({user_role} at {organization}). [Speaker N] are distinct remote speakers. Infer from content which speakers are {organization} employees (present {organization}'s offerings, speak on behalf of {organization}, talk about {organization}-internal programs or pricing) versus {counterpart_label} (ask questions about their own environment, describe their internal systems, raise their own constraints). Use actual names when the transcript reveals them via introductions or direct address.

Rules:
- Never say "the customer", "the SA", "the attendee", "the provider" — use actual names. If a speaker's name is not revealed in the transcript, use their speaker label as-is (e.g. [Speaker 2]). Never invent names.
- Short bullets. No multi-sentence paragraphs. No topic-sentence + elaboration pattern.
- Skip filler: greetings, scheduling talk, audio issues, recap of slides already shown on-screen.
- Omit anything that was not actually said. Do not extrapolate.
- Include specifics when they were mentioned: dollar amounts, dates, system names, use case names, team names.
- Do not write "No X" for empty sections — omit the section entirely.

Format:

## Participants
Two sub-groups as bulleted lists.
**{organization}:** names (role if mentioned).
**{counterpart_label}:** names (org and role if mentioned).
If a speaker cannot be classified from the transcript, list them under **Unclear:** rather than guess.

## Notes

### {organization}
Key points {organization} people made. One bullet per point. When a point is tied to a specific person, prefix the bullet with **Name** — . When a point is a shared {organization} position or the speaker is unclear, skip the attribution.

### {counterpart_label}
Key points the {counterpart_label} side made. Same rules as above. When a single person had a coherent thread covering several related points, group those under a sub-heading with their name.

## Action Items
Numbered list. Each item: **Owner** — terse verb phrase. Include deadline if mentioned in the transcript. Omit this section entirely if nothing was assigned.

Transcript:
{transcript}
