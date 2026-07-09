from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

ENV_FILE = Path(".env")
TOKEN_FILE = Path("credentials/.tiktok_token.json")
DEFAULT_REDIRECT_URI = "http://localhost:8000/callback"
DEFAULT_SCOPES = "user.info.basic,video.publish,video.upload"




def decorate_token_data(token_data: dict[str, object]) -> dict[str, object]:
    now = int(time.time())
    output = dict(token_data)
    output["saved_at"] = now
    output["saved_at_iso"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    try:
        expires_in = int(output.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        output["expires_at"] = now + expires_in
        output["expires_at_iso"] = datetime.fromtimestamp(now + expires_in, tz=timezone.utc).isoformat()

    try:
        refresh_expires_in = int(output.get("refresh_expires_in") or 0)
    except (TypeError, ValueError):
        refresh_expires_in = 0
    if refresh_expires_in > 0:
        output["refresh_expires_at"] = now + refresh_expires_in
        output["refresh_expires_at_iso"] = datetime.fromtimestamp(now + refresh_expires_in, tz=timezone.utc).isoformat()

    return output


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def set_env_value(key: str, value: str, path: Path = ENV_FILE) -> None:
    lines: list[str] = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    output: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            output.append(f"{key}={value}\n")
            found = True
        else:
            output.append(line)

    if not found:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.append(f"{key}={value}\n")

    path.write_text("".join(output), encoding="utf-8")


def generate_code_verifier(length: int = 64) -> str:
    chars = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(chars) for _ in range(length))


def generate_code_challenge(verifier: str) -> str:
    # TikTok Desktop Login PKCE usa SHA256 em HEX, não base64url.
    # Se esse valor não bater exatamente com o code_verifier enviado no token exchange,
    # o TikTok retorna: "Code verifier or code challenge is invalid."
    return hashlib.sha256(verifier.encode("utf-8")).hexdigest()


def main() -> None:
    env = load_env()

    client_key = env.get("TIKTOK_CLIENT_ID") or env.get("TIKTOK_CLIENT_KEY")
    client_secret = env.get("TIKTOK_CLIENT_SECRET")
    redirect_uri = env.get("TIKTOK_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    scopes = env.get("TIKTOK_SCOPES", DEFAULT_SCOPES)

    if not client_key or not client_secret:
        print("Erro: configure TIKTOK_CLIENT_ID e TIKTOK_CLIENT_SECRET no arquivo .env")
        sys.exit(1)

    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.hostname not in {"localhost", "127.0.0.1"}:
        print("Erro: para este script local, use localhost ou 127.0.0.1 no TIKTOK_REDIRECT_URI.")
        sys.exit(1)
    if not parsed_redirect.port:
        print("Erro: TIKTOK_REDIRECT_URI precisa ter porta. Exemplo: http://localhost:8000/callback")
        sys.exit(1)

    callback_path = parsed_redirect.path or "/callback"
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(24)

    auth_params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(auth_params)

    result: dict[str, str | None] = {"code": None, "error": None, "state": None, "scopes": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed_request = urlparse(self.path)
            if parsed_request.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Callback path incorreto.")
                return

            query = parse_qs(parsed_request.query)
            result["code"] = query.get("code", [None])[0]
            result["error"] = query.get("error", [None])[0]
            result["state"] = query.get("state", [None])[0]
            result["scopes"] = query.get("scopes", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"]:
                message = f"""
                <h2>Erro ao autorizar TikTok</h2>
                <p>{result['error']}</p>
                <p>Você pode fechar esta janela.</p>
                """
            else:
                message = """
                <h2>TikTok autorizado com sucesso</h2>
                <p>Você pode fechar esta janela e voltar ao terminal.</p>
                """
            self.wfile.write(message.encode("utf-8"))

    server = HTTPServer((parsed_redirect.hostname, parsed_redirect.port), CallbackHandler)

    print("\nAbrindo autorização do TikTok no navegador...")
    print("\nSe não abrir sozinho, copie e cole este link no navegador:\n")
    print(auth_url)
    print("\nAguardando callback do TikTok...\n")

    webbrowser.open(auth_url)
    server.handle_request()

    if result["error"]:
        print("Erro retornado pelo TikTok:", result["error"])
        sys.exit(1)
    if result["state"] != state:
        print("Erro: state recebido não confere. Processo cancelado por segurança.")
        sys.exit(1)
    if not result["code"]:
        print("Erro: nenhum code foi recebido.")
        sys.exit(1)

    print("Code recebido. Trocando por access_token...")
    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=60,
    )

    try:
        token_data = response.json()
    except ValueError:
        print("Erro: resposta do TikTok não veio em JSON.")
        print(response.status_code)
        print(response.text)
        sys.exit(1)

    if response.status_code >= 400 or "access_token" not in token_data:
        print("\nErro ao gerar token:")
        print(json.dumps(token_data, indent=2, ensure_ascii=False))
        sys.exit(1)

    token_data = decorate_token_data(token_data)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2, ensure_ascii=False), encoding="utf-8")

    set_env_value("TIKTOK_ACCESS_TOKEN", str(token_data.get("access_token", "")))
    set_env_value("TIKTOK_REFRESH_TOKEN", str(token_data.get("refresh_token", "")))
    set_env_value("TIKTOK_OPEN_ID", str(token_data.get("open_id", "")))
    set_env_value("TIKTOK_TOKEN_FILE", str(TOKEN_FILE).replace("\\", "/"))

    print("\nToken gerado com sucesso.")
    print(f"Arquivo salvo: {TOKEN_FILE}")
    print("Também atualizei o .env com:")
    print("- TIKTOK_ACCESS_TOKEN")
    print("- TIKTOK_REFRESH_TOKEN")
    print("- TIKTOK_OPEN_ID")
    print("- TIKTOK_TOKEN_FILE")

    granted_scope = token_data.get("scope", "")
    print("\nEscopos autorizados:")
    print(granted_scope)

    missing = [scope for scope in ("video.publish", "video.upload") if scope not in granted_scope]
    if missing:
        print("\nAtenção: o token não veio com estes escopos:", ", ".join(missing))
        print("Sem video.publish, Direct Post não funciona. Sem video.upload, Inbox/Draft não funciona.")

    print("\nConcluído.")


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    main()
