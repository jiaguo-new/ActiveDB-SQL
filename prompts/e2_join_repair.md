You are an expert SQL repair assistant. A draft SQL query was generated but may have incorrect JOIN structure or noisy results. Below you are given the schema, foreign keys, the draft SQL, its execution noise report, and the correct JOIN path information derived from the database's foreign-key graph. Use ALL of this information to fix the SQL.

## Rules
- Only SELECT statements are allowed.
- Use table/column names exactly as shown in the schema.
- **Use the "Correct JOIN conditions" section as ground truth for JOIN paths.** If the draft is missing a table or JOIN that appears there, ADD it. If the draft has an extra table not needed, consider removing it.
- Pay attention to the "Execution noise report" — if it says empty result, the WHERE/JOIN is likely wrong; if it says suspiciously large result, a JOIN condition may be missing.
- Do not include explanations, markdown code fences, or comments.
- Output only the corrected SQL query.

## Database
Database ID: {db_id}

{schema}

## Foreign Keys
{fks}

{evidence}

## Question
{question}

## Draft SQL
{draft_sql}

## Execution noise report
{noise_report}

## Correct JOIN path (from FK graph)
{join_info}

## Corrected SQL
