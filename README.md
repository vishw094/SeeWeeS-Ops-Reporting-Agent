# MSBA AI Agents Demo (LangGraph + LangChain)

Multi-agent system for operations/dispatch planning:
- Reads business context & KPI definitions from a PDF (RAG)
- Analyzes ops data from CSV (KPIs + anomaly detection)
- Pulls weather forecast and derives dispatch risk
- Produces a leadership-ready report
- Emails the report via Gmail SMTP (app password)

## Project Structure
- `data/` input PDF + CSV
- `src/` application code
- `chroma_db/` local vector store (not committed)
- `.env` secrets (not committed)

## Setup
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill OPENAI_API_KEY and Gmail app password
