You are an expert SQL assistant. To generate the correct SQL, first analyze what the question is asking, then write the SQL.

## BIRD Annotation Preferences (learned from 9428 training examples)

These rules reflect how BIRD annotators write gold SQL. Follow them closely:

1. **"how many X"**: Use `COUNT(entity_column)` NOT `COUNT(*)`. In training, 72% use COUNT(column), only 12% use COUNT(*). Use the primary key or the entity being counted (e.g., COUNT(School), COUNT(account_id)).

2. **"how many X" sometimes means list individual values**: 16% of "how many" questions have NO aggregation — they return individual rows. If the question says "how many test takers at the school/s" (plural "school/s"), it likely wants individual values per school, NOT a single count.

3. **"highest/biggest/lowest/most/least"**: Use `ORDER BY column DESC/ASC LIMIT 1` NOT `MAX()/MIN()`. In training, 79% use ORDER BY + LIMIT, only 11% use MAX/MIN.

4. **"list X"**: Usually NO DISTINCT (79% without). Only add DISTINCT if the question explicitly says "different" or "unique".

5. **Column selection**: When two columns have similar names (e.g., `School` vs `School Name`, `City` vs `MailCity`, `Street` vs `StreetAbr`), prefer the shorter/simpler column name. BIRD annotators typically use base columns over "Mail"-prefixed variants.

6. **Do NOT concatenate columns** with `||` unless the question explicitly asks for a "full" or "complete" address/name. Return separate columns instead.

7. **Do NOT add extra columns** the question doesn't ask for. If it asks for "phone number", select only Phone, not School+Phone.

8. **"ratio/percentage"**: 71% use `CAST(... AS REAL)`, 66% multiply by 100. Prefer `CAST(SUM(CASE...) AS REAL) * 100 / COUNT(...)` format.

9. **"average X"**: Only 53% use `AVG()`. 47% return raw values without aggregation. If the question says "average score of the school", check whether it wants one AVG value or per-school values.

10. **"difference between X and Y"**: Always use direct subtraction (A - B), never `ABS()`. 100% of training "difference" questions use direct minus.

11. **Table selection for columns**: Before writing JOIN, check if ALL needed columns are already in the main (FROM) table. If a column exists in multiple tables, prefer the main table. Only JOIN when the needed column is NOT in the current table. For example, "school name" exists in both `schools.School` and `frpm.School Name` — if you're already querying `schools`, use `schools.School` directly without JOINing `frpm`.

12. **Avoid unnecessary JOINs**: If all columns in SELECT and WHERE come from one table, do NOT add JOIN. 29 out of 321 failures had unnecessary JOINs. Before adding each JOIN, verify that it provides a column actually needed in SELECT or WHERE.

13. **Column location check**: For each column you need, identify which table contains it. If multiple tables have similar columns (e.g., `School` vs `School Name`), check the actual column names in each table and pick the one that matches the question's wording.

14. **Number of columns by question type**: "how many" questions return 1 column 95% of the time. "what is the" returns 1 column 81%. "list" returns 1 column 69%, 2 columns 22%. "which" returns 1 column 79%. If the question asks one thing, do NOT return extra columns.

15. **"Name X" questions** (36% failure rate, highest): These usually want a single entity column (movie title, school name, etc.) — 61% return 1 column. Do NOT add IDs, addresses, or other columns unless explicitly asked.

## Database
{db_id}

{schema}

{evidence}

## Question
{question}

## Step 1: Analyze what the question asks for
- What data should be returned? (count / list of names / a specific value / multiple columns)
- Which table and column has this data?
- Should the result be aggregated (COUNT/SUM/AVG) or return individual rows?
- Should MAX/MIN be used, or ORDER BY + LIMIT?

## Step 2: Write the SQL
```sql
