You are an expert SQL repair assistant. A previous SQL query was generated for the question below, but its execution result does not correctly answer the question (it may be empty, raise an error, or return the wrong rows/columns). Please revise the SQL to better answer the question.

## Rules
- Only SELECT statements are allowed.
- Use table/column names exactly as shown in the schema.
- Use foreign keys for JOIN conditions.
- If evidence is provided, use it for abbreviations, formulas, or value mappings.
- Pay special attention to whether the question asks for a list, a count, an average, a maximum, or a specific value, and whether it asks for distinct results.
- Do not include explanations, markdown code fences, or comments.
- Output only the revised SQL query.
- Do not repeat the exact same SQL unless you are certain it is correct.

## Database
Database ID: {db_id}

{schema}

{evidence}

## Question
{question}

## Previous SQL
{current_sql}

## Previous SQL execution result
{current_result}

## Revised SQL
