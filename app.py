import os
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import clickhouse_connect
from typing import Optional
from langfuse import get_client, propagate_attributes
from openai import OpenAI

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

client = clickhouse_connect.get_client(
    host=os.environ['CH_HOST'],
    port=8443,
    username=os.environ.get('CH_USER', 'default'),
    password=os.environ['CH_PASSWORD'],
    secure=True
)

langfuse = get_client()

llm_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ['OPENROUTER_API_KEY']
)

SYSTEM_PROMPT = """You are a SQL expert. You convert natural language questions into ClickHouse SQL queries.

The database has one table: perfume_db.catalog with these columns:
- id (UInt32)
- name (String) - perfume name
- brand (String) - brand name
- category (LowCardinality(String)) - e.g. 'eau de parfum', 'eau de toilette', 'concentrated perfume oil'
- gender (LowCardinality(String)) - 'men', 'women', 'unisex'
- top_notes (Array(String))
- heart_notes (Array(String))
- base_notes (Array(String))
- price_usd (Float32)
- rating (Float32) - 0 to 5
- launch_year (UInt16)
- longevity_hours (Float32)
- season (LowCardinality(String)) - 'spring', 'summer', 'fall', 'winter', 'all-season'

Rules:
- Return ONLY the SQL query, no explanation, no markdown, no backticks
- Use ClickHouse SQL syntax
- For note searches, use arrayExists(x -> x ILIKE '%term%', arrayConcat(top_notes, heart_notes, base_notes))
- Always ORDER BY rating DESC unless the user asks for something else
- LIMIT 20 unless the user specifies
"""

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]
    return templates.TemplateResponse(request=request, name="index.html", context={
        "brands": brands, "seasons": seasons,
        "perfumes": [], "stats": None, "filters": {},
        "ai_query": "", "ai_sql": ""
    })

@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: Optional[str] = "",
    brand: Optional[str] = "",
    gender: Optional[str] = "",
    season: Optional[str] = "",
    min_price: Optional[float] = 0,
    max_price: Optional[float] = 500,
    min_rating: Optional[float] = 0,
):
    filters_input = {"q": q, "brand": brand, "gender": gender, "season": season,
                     "min_price": min_price, "max_price": max_price, "min_rating": min_rating}

    with langfuse.start_as_current_observation(
        as_type="span", name="perfume-search", input=filters_input,
    ) as root_span:
        with propagate_attributes(tags=["perfume-explorer"], metadata={"source": "web-ui"}):

            conditions = [
                f"price_usd BETWEEN {min_price} AND {max_price}",
                f"rating >= {min_rating}",
            ]
            if brand:
                conditions.append(f"brand = '{brand}'")
            if gender:
                conditions.append(f"gender = '{gender}'")
            if season:
                conditions.append(f"season = '{season}'")
            if q:
                safe_q = q.replace("'", "\\'")
                conditions.append(
                    f"(name ILIKE '%{safe_q}%' OR brand ILIKE '%{safe_q}%' "
                    f"OR arrayExists(x -> x ILIKE '%{safe_q}%', arrayConcat(top_notes, heart_notes, base_notes)))"
                )

            where = " AND ".join(conditions)
            query = f"""
                SELECT name, brand, category, gender,
                       arrayStringConcat(top_notes, ', ') AS top,
                       arrayStringConcat(heart_notes, ', ') AS heart,
                       arrayStringConcat(base_notes, ', ') AS base,
                       price_usd, rating, launch_year, longevity_hours, season
                FROM perfume_db.catalog
                WHERE {where}
                ORDER BY rating DESC
            """

            with langfuse.start_as_current_observation(
                as_type="span", name="clickhouse-query", input={"sql": query}
            ) as query_span:
                start = time.time()
                rows = client.query(query).result_rows
                duration_ms = round((time.time() - start) * 1000, 2)
                query_span.update(output={"row_count": len(rows), "duration_ms": duration_ms})

            perfumes = [
                {"name": r[0], "brand": r[1], "category": r[2], "gender": r[3],
                 "top": r[4], "heart": r[5], "base": r[6], "price": r[7],
                 "rating": r[8], "year": r[9], "longevity": r[10], "season": r[11]}
                for r in rows
            ]

            stats_query = f"""
                SELECT count(), round(avg(price_usd),0), round(avg(rating),1), round(avg(longevity_hours),0)
                FROM perfume_db.catalog WHERE {where}
            """
            with langfuse.start_as_current_observation(
                as_type="span", name="clickhouse-stats-query", input={"sql": stats_query}
            ) as stats_span:
                s = client.query(stats_query).result_rows[0]
                stats_span.update(output={"count": s[0], "avg_price": s[1], "avg_rating": s[2], "avg_longevity": s[3]})

            stats = {"count": s[0], "avg_price": s[1], "avg_rating": s[2], "avg_longevity": s[3]}
            root_span.update(output={"results_count": len(perfumes), "query_duration_ms": duration_ms})

    langfuse.flush()

    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons_list = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]

    return templates.TemplateResponse(request=request, name="index.html", context={
        "brands": brands, "seasons": seasons_list,
        "perfumes": perfumes, "stats": stats,
        "filters": filters_input, "ai_query": "", "ai_sql": ""
    })


@app.get("/ask", response_class=HTMLResponse)
def ask_ai(request: Request, question: str = ""):
    if not question.strip():
        return templates.TemplateResponse(request=request, name="index.html", context={
            "brands": [], "seasons": [], "perfumes": [], "stats": None,
            "filters": {}, "ai_query": "", "ai_sql": ""
        })

    with langfuse.start_as_current_observation(
        as_type="span", name="text-to-sql", input={"question": question},
    ) as root_span:
        with propagate_attributes(tags=["perfume-explorer", "text-to-sql"], metadata={"source": "ai-search"}):

            # Step 1: LLM generates SQL
            with langfuse.start_as_current_observation(
                as_type="generation", name="llm-sql-generation",
            ) as gen_span:
                gen_span.update(input={"system": SYSTEM_PROMPT, "user": question}, model="openai/gpt-oss-20b:free")

                response = llm_client.chat.completions.create(
                    model="openai/gpt-oss-20b:free",
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question}
                    ],
                    max_tokens=500,
                    temperature=0
                )
                generated_sql = response.choices[0].message.content.strip().strip('`').replace('sql\n', '').strip()

                gen_span.update(
                    output=generated_sql,
                    usage_details={
                        "input_tokens": response.usage.prompt_tokens or 0,
                        "output_tokens": response.usage.completion_tokens or 0,
                    }
                )

            # Step 2: Execute the generated SQL on ClickHouse
            perfumes = []
            error_msg = ""
            with langfuse.start_as_current_observation(
                as_type="span", name="clickhouse-ai-query", input={"sql": generated_sql}
            ) as ch_span:
                try:
                    start = time.time()
                    rows = client.query(generated_sql).result_rows
                    duration_ms = round((time.time() - start) * 1000, 2)

                    for r in rows:
                        if len(r) >= 12:
                            perfumes.append({
                                "name": r[0], "brand": r[1], "category": r[2], "gender": r[3],
                                "top": r[4] if isinstance(r[4], str) else ', '.join(r[4]) if isinstance(r[4], list) else str(r[4]),
                                "heart": r[5] if isinstance(r[5], str) else ', '.join(r[5]) if isinstance(r[5], list) else str(r[5]),
                                "base": r[6] if isinstance(r[6], str) else ', '.join(r[6]) if isinstance(r[6], list) else str(r[6]),
                                "price": r[7], "rating": r[8], "year": r[9],
                                "longevity": r[10], "season": r[11]
                            })
                        else:
                            perfumes.append({
                                "name": str(r[0]) if len(r) > 0 else "",
                                "brand": str(r[1]) if len(r) > 1 else "",
                                "category": str(r[2]) if len(r) > 2 else "",
                                "gender": "", "top": "", "heart": "", "base": "",
                                "price": r[3] if len(r) > 3 else 0,
                                "rating": r[4] if len(r) > 4 else 0,
                                "year": "", "longevity": "", "season": ""
                            })

                    ch_span.update(output={"row_count": len(rows), "duration_ms": duration_ms})
                except Exception as e:
                    error_msg = str(e)
                    ch_span.update(output={"error": error_msg})

            root_span.update(output={
                "generated_sql": generated_sql,
                "results_count": len(perfumes),
                "error": error_msg
            })

    langfuse.flush()

    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons_list = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]

    return templates.TemplateResponse(request=request, name="index.html", context={
        "brands": brands, "seasons": seasons_list,
        "perfumes": perfumes, "stats": None,
        "filters": {}, "ai_query": question, "ai_sql": generated_sql
    })
