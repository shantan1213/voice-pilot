# ⚙️ Configuration and environment

[`app/settings.py`](../app/settings.py) creates a `Settings` object with `pydantic-settings`. It reads `.env` as UTF-8, ignores extra keys, and defines:

| Variable | Default | Use |
|---|---|---|
| `MODEL_API_KEY` | empty | Meta ASR and Muse Spark authorization |
| `MUSE_CHAT_MODEL` | `muse-spark-1.3` | Muse Spark model name |
| `APP_ENV` | `dev` | Available setting; current routing does not branch on it |

`main.py` also calls `load_dotenv()` and reads `MODEL_API_KEY`/`MUSE_CHAT_MODEL` for active route constants. The extracted chat router falls back to `settings`, but it is not registered. Set variables before starting Uvicorn; restart after edits.

```powershell
Copy-Item .env.example .env
# edit .env and set MODEL_API_KEY privately
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

The default local bind is 127.0.0.1:8002 in the documented startup command. MMS model files are downloaded by Transformers and cached outside the repository. Optional CUDA support depends on the installed PyTorch build. Never commit `.env` or paste a key into browser code/logs.
