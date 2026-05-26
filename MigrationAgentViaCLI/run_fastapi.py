import uvicorn


if __name__ == "__main__":
    uvicorn.run("migration_agent_cli.api.main:app", host="127.0.0.1", port=8065, reload=False)

