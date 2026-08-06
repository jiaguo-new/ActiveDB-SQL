You are an expert SQL assistant. A draft SQL query was generated but may have incorrect column selection in the SELECT clause. You are given the schema, the draft SQL, its execution result, and a column-grounding report that identifies suspicious column choices and suggests alternatives based on the actual database column names and sample values. Use this information to fix the SELECT clause.

## Rules
- Only SELECT statements are allowed.
- Use table/column names exactly as shown in the schema.
- **Pay special attention to the "Column grounding report"**: it identifies columns that don't match the question's intent and suggests alternatives. If a suggested alternative matches the question better, use it.
- **For COUNT(\*)**: if the question asks "how many X" and the report suggests COUNT(entity_column), use COUNT(entity_column) instead of COUNT(*), because COUNT(*) includes NULL rows while COUNT(entity_column) does not.
- **Do not add extra columns** that the question doesn't ask for. If the question asks for "phone number", select only the phone column, not the school name too.
- **Do not concatenate columns** (using ||) unless the question explicitly asks for a combined/full value.
- Keep the WHERE, JOIN, GROUP BY, ORDER BY, LIMIT clauses unchanged unless they are clearly wrong.
- Do not include explanations, markdown code fences, or comments.
- Output only the corrected SQL query.

## Database
Database ID: {db_id}

{schema}

{evidence}

## Question
{question}

## Draft SQL
{draft_sql}

## Draft SQL execution result
{draft_result}

## Column grounding report (column-selection diagnosis)
{column_report}

## Corrected SQL
