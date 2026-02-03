# Agentic AI Demo with MCP & MongoDB Atlas

A production-ready multi-agent system showcasing:
- **Semantic Routing** via MongoDB Vector Search
- **Agentic Memory** (Long-term user preferences)
- **Multi-Domain Intelligence** (Identity, Finance, Lifestyle)
- **Multi-Agent Collaboration** (Worker + Critic pattern)
- **Live Observability** (Real-time broadcast to mobile devices)

## Prerequisites

- Python 3.12+
- MongoDB Atlas cluster (free tier works)
- OpenAI API key
- uv (Python package installer): `pip install uv`

## Setup

### 1. Install Dependencies
```bash
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

### 2. Configure MongoDB Atlas

Create a free cluster at [mongodb.com/cloud/atlas](https://mongodb.com/cloud/atlas)

**Create Vector Search Index:**
1. Go to your cluster → "Atlas Search"
2. Create index on database `agent_brain`, collection `episodic_memory`
3. Use this JSON configuration:
```json
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 1536,
      "similarity": "cosine"
    }
  ]
}
```

**Create second index** on database `agent_registry`, collection `mcp_services` (same config).

### 3. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

Edit `.env` with your actual keys.

### 4. Run the Demo

```bash
uv run main.py
```

## Live Broadcast Feature

During the demo, open this URL on mobile devices to see agent thoughts in real-time:
**https://ntfy.sh/agent_demo_live_stream_2025**

## Example Queries

```
> Hi, I'm vegan and 30 years old
> Find me a restaurant for dinner
> What's the current Solana price in USD?
> Send an alert if SOL is over $100
> memory (see what the agent remembers)
> status (check system health)
```

## Architecture

- **Orchestrator:** Central brain coordinating all agents
- **Semantic Router:** MongoDB vector search selects relevant experts
- **MCP Servers:** Isolated Python processes (stdio protocol)
- **Multi-Agent:** Worker generates, Critic reviews for safety
- **Memory Store:** MongoDB Atlas with embeddings

## Troubleshooting

- **"uv not found":** Install with `pip install uv`
- **MongoDB connection fails:** Check connection string in `.env`
- **Vector search fails:** Wait 5 minutes for Atlas index to build

## License

MIT
