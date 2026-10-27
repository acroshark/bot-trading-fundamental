"""Prepara un OpenClaw de trading aislado: estado y espacio de trabajo en M:, fuera de OneDrive, y sin tocar el OpenClaw de la fábrica.

Uso: M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe instalar.py [--force]
--force regenera openclaw.json conservando el token del panel.
"""
import json
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = Path(r"M:\BotFundamental-openclaw")
STATE, WORK = BASE / "state", BASE / "workspace"
PORT = 18790   # la fábrica usa 18789
LLAMA = Path(r"M:\OneDrive\herramientas\IA local LLM\llama\llama-server.exe")
MODEL = Path(r"M:\OneDrive\herramientas\IA local LLM\modelos\Qwen3.5-9B-Q4_K_M.gguf")
PY = "M:/BotFundamental-runtime/venv/Scripts/python.exe"
RADAR = r"M:\\BotFundamental-openclaw\\workspace\\skills\\radar\\scripts\\radar\.py"   # regex: \\ = una barra literal
# Mismos permisos denegados que la fábrica: sin web, mensajería, automatizaciones ni subagentes.
DENY = ["group:web", "group:ui", "group:messaging", "group:nodes", "group:automation", "group:sessions", "group:media",
        "group:agents", "group:plugins", "group:memory", "process", "code_execution", "secrets", "apply_patch", "browser",
        "web_fetch", "web_search", "x_search", "canvas", "terminal", "computer", "gateway", "nodes", "message", "cron",
        "plugins", "openclaw", "sessions_spawn", "sessions_send", "subagents", "agents_wait", "image_generate",
        "video_generate", "music_generate", "tts", "view_image", "pdf", "file_fetch", "dir_list", "dir_fetch", "file_write",
        "bundle-mcp", "skill_workshop", "suggest_task", "conversations_send", "conversations_turn"]
EXEC = {"host": "gateway", "mode": "allowlist", "strictInlineEval": True, "safeBins": [], "timeoutSeconds": 120,
        "applyPatch": {"enabled": False, "workspaceOnly": True}}


def config(token: str) -> dict:
    return {
        "gateway": {"mode": "local", "auth": {"mode": "token", "token": token}, "port": PORT, "bind": "loopback",
                    "tailscale": {"mode": "off"}, "controlUi": {"automaticallyFetchFavicons": False}},
        "models": {"mode": "merge", "providers": {"llama-cpp": {
            "baseUrl": "http://127.0.0.1:8094/v1", "api": "openai-completions", "request": {"allowPrivateNetwork": True},
            "timeoutSeconds": 600,
            "models": [{"id": "qwen-trading", "name": "qwen-trading", "reasoning": False, "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 32768, "contextTokens": 32768, "maxTokens": 4096,
                        "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False,
                                   "supportsTemperature": True, "supportsUsageInStreaming": True, "supportsTools": True,
                                   "supportsStrictMode": False, "supportsJsonSchemaResponseFormat": True,
                                   "requiresStringContent": True, "maxTokensField": "max_tokens"},
                        "params": {"modelPath": str(MODEL)}}],
            # Si el bot no lo tiene encendido, OpenClaw arranca el mismo servidor (puerto 8094, nombre qwen-trading).
            "localService": {"command": str(LLAMA),
                             "args": ["-m", str(MODEL), "--alias", "qwen-trading", "-ngl", "99", "-c", "32768", "-np", "1",
                                      "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--host", "127.0.0.1", "--port", "8094",
                                      "--reasoning", "off", "--no-webui"],
                             "cwd": str(LLAMA.parent), "healthUrl": "http://127.0.0.1:8094/health",
                             "readyTimeoutMs": 300000, "idleStopMs": 900000}}}},
        "plugins": {"entries": {"llama-cpp": {"enabled": True}, "codex": {"enabled": False}, "browser": {"enabled": False},
                                "canvas": {"enabled": False}, "cua-computer": {"enabled": False},
                                "memory-core": {"config": {"dreaming": {"enabled": False}}}}},
        "agents": {
            "defaults": {"workspace": str(WORK), "model": {"primary": "llama-cpp/qwen-trading"}, "heartbeat": {"every": "0m"},
                         "systemAgent": {"agentId": "trading"}, "sandbox": {"mode": "off"}, "elevatedDefault": "off",
                         "timeoutSeconds": 900, "skipBootstrap": True},
            "ownership": "explicit",
            "entries": {"trading": {"name": "Analista de mercado", "workspace": str(WORK),
                                    "agentDir": str(STATE / "agents" / "trading" / "agent"), "identity": {"name": "trading"},
                                    "skills": ["radar"],
                                    "tools": {"allow": ["exec", "read"], "fs": {"workspaceOnly": True}, "exec": EXEC}}}},
        "tools": {"profile": "coding", "deny": DENY, "toolSearch": False, "codeMode": False, "swarm": False,
                  "fs": {"workspaceOnly": True}, "exec": EXEC, "elevated": {"enabled": False},
                  "agentToAgent": {"enabled": False}, "sessions": {"visibility": "self"},
                  "web": {"search": {"enabled": False}, "fetch": {"enabled": False}}, "links": {"enabled": False}},
    }


def approvals() -> dict:
    pre = f"^-I -B {RADAR} "
    return {"version": 1,
            "defaults": {"security": "deny", "ask": "off", "askFallback": "deny", "autoAllowSkills": False},
            "agents": {"trading": {"security": "allowlist", "ask": "off", "askFallback": "deny", "autoAllowSkills": False,
                                   "allowlist": [{"pattern": PY, "argPattern": pre + "(estado|flujo|agenda|balanza|tendencia|predicciones|backtest|cartera|aperturas)$"},
                                                 {"pattern": PY, "argPattern": pre + "noticias( --horas ([1-9]|[1-3][0-9]|4[0-8]))?( --min ([0-9]|10))?"
                                                  "( --bolsa (tokio|seul|shanghai|hongkong|dubai|frankfurt|londres|madrid|nuevayork|saopaulo))?$"}]}}}


def main() -> None:
    force = "--force" in sys.argv
    for d in (STATE, WORK, STATE / "agents" / "trading" / "agent"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copytree(HERE / "workspace", WORK, dirs_exist_ok=True)   # AGENTS.md y skills; la memoria del agente se conserva
    cfg_path = STATE / "openclaw.json"
    token = None
    if cfg_path.exists():
        token = json.loads(cfg_path.read_text(encoding="utf-8")).get("gateway", {}).get("auth", {}).get("token")
    if force or not cfg_path.exists():
        cfg_path.write_text(json.dumps(config(token or secrets.token_hex(24)), indent=2, ensure_ascii=False), encoding="utf-8")
        print("Configuración escrita en", cfg_path)
    else:
        print("La configuración ya existía; usa --force para regenerarla.")
    (STATE / "aprobaciones.json").write_text(json.dumps(approvals(), indent=2), encoding="utf-8")
    print("Espacio de trabajo:", WORK)
    print("Siguiente paso: arrancar-openclaw.ps1")


if __name__ == "__main__":
    main()
