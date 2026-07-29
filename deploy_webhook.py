#!/usr/bin/env python3
"""
WEBHOOK DE DEPLOY - GARRABOT
Sobe um servidor HTTP na porta 9000.
Quando recebe POST /deploy com o token certo, roda git pull + pm2 restart.

Como rodar no servidor:
  pm2 start deploy_webhook.py --name deploy-webhook --interpreter python3

Como chamar (do navegador ou de qualquer lugar):
  curl -X POST http://158.101.108.207:9000/deploy \
       -H "Content-Type: application/json" \
       -d '{"token": "TROQUE_ESSE_TOKEN_AQUI"}'
"""

import http.server
import json
import subprocess
import logging
import os

# ─── CONFIGURAÇÃO ────────────────────────────────────────────────────
PORTA = 9000
TOKEN_SECRETO = "TROQUE_ESSE_TOKEN_AQUI"   # <-- mude para algo seguro
PASTA_PROJETO = "/home/ubuntu/CLIEMTE-garrabot"
NOME_PM2 = "garrabot"
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DEPLOY] %(message)s")


class DeployHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logging.info("%s - %s", self.address_string(), format % args)

    def _responder(self, status, mensagem):
        corpo = json.dumps({"status": mensagem}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(corpo))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        """Rota de health-check: GET /health"""
        if self.path == "/health":
            self._responder(200, "webhook online")
        else:
            self._responder(404, "rota nao encontrada")

    def do_POST(self):
        if self.path != "/deploy":
            self._responder(404, "rota nao encontrada")
            return

        tamanho = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(tamanho))
        except Exception:
            self._responder(400, "body JSON invalido")
            return

        if body.get("token") != TOKEN_SECRETO:
            logging.warning("Token invalido recebido de %s", self.client_address)
            self._responder(403, "token invalido")
            return

        logging.info("Deploy autorizado - iniciando...")
        try:
            resultado = subprocess.run(
                f"cd {PASTA_PROJETO} && git pull && pm2 restart {NOME_PM2}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=60
            )
            saida = resultado.stdout + resultado.stderr
            logging.info("Saida do deploy:\n%s", saida)
            self._responder(200, "deploy concluido")
        except subprocess.TimeoutExpired:
            self._responder(500, "timeout no deploy")
        except Exception as e:
            self._responder(500, f"erro: {str(e)}")


if __name__ == "__main__":
    servidor = http.server.HTTPServer(("0.0.0.0", PORTA), DeployHandler)
    logging.info("Webhook de deploy escutando na porta %d", PORTA)
    logging.info("Endpoint: POST http://<IP>:%d/deploy", PORTA)
    servidor.serve_forever()
