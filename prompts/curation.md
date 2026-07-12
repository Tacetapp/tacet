You are curating a live meeting insights panel. Review these insights and identify which ones to REMOVE because they are:
- Stale (topic has moved on, no longer relevant to current discussion)
- Redundant (duplicates or near-duplicates of other insights)
- Low-value (obvious, generic, or not actionable)
- Suggested questions that are no longer timely

Current discussion context (last ~100 segments):
{recent_transcript}

Current insights (id: category — text):
{insights_list}

Return a JSON array of insight IDs to remove. Example: [3, 7, 12]
If nothing should be removed, respond with: []