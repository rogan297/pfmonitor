#!/usr/bin/env python3
"""
Robô de Agendamento PF
1º checa API a cada 5 min (sem abrir navegador)
2º quando achar vaga, abre navegador pra agendar
"""
import asyncio
import json
import sys
import time
import datetime
import subprocess
import argparse
import socket
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# === CONFIG ===
BASE_URL = "https://servicos.pf.gov.br/agenda-publico-rest/api"
URL_ACCESS = "https://servicos.pf.gov.br/agenda-web/acessar"
CODIGO_SOLICITACAO = "202511201800457713"
DATA_NASCIMENTO = "03/05/2006"
DATA_NASCIMENTO_API = "2006-05-03"
SISTEMA_ID = 1
TIPO_SERVICO_ID = 11
UNIDADES = [
    (13, "PAE Viracopos (Campinas)"),
    (531, "NUMIG Viracopos (Campinas)"),
    (134, "DPF Sorocaba"),
]
NTFY_TOPIC = "pfagendamento_script"

# === NOTIFY ===
def notify(msg, urgent=True):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[{ts}] {msg}")
    print(f"{'='*60}\n")
    sys.stdout.write("\a")
    sys.stdout.flush()
    try:
        level = "critical" if urgent else "normal"
        subprocess.run(["notify-send", "PF Agendamento", msg, "-u", level],
                      timeout=5, capture_output=True)
    except:
        pass
    try:
        data = json.dumps({
            "topic": NTFY_TOPIC, "title": "PF Agendamento",
            "message": msg, "priority": 5 if urgent else 3,
            "tags": ["rotating_light" if urgent else "bell"],
        }).encode()
        req = Request("https://ntfy.sh", data=data,
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
    except Exception as e:
        print(f"   ntfy.sh error: {e}")

# === API ===
def api_get(path):
    try:
        req = Request(f"{BASE_URL}/{path}", headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as r:
            body = r.read().decode()
            if not body:
                return None
            try:
                return json.loads(body) if body else None
            except json.JSONDecodeError:
                return body
    except (HTTPError, URLError, socket.timeout, TimeoutError, OSError):
        return None

def check_api():
    req_data = api_get(f"agendamento/buscarRequerente/{DATA_NASCIMENTO_API}/{CODIGO_SOLICITACAO}/{SISTEMA_ID}")
    if not req_data:
        return "requerente_nao_encontrado", None

    ag = api_get(f"agendamento/buscarAgendamento?dataNascimento={DATA_NASCIMENTO_API}&codigoSolicitacao={CODIGO_SOLICITACAO}")
    if ag == "HAS_AGENDAMENTO":
        return "ja_agendado", req_data

    for uid, nome in UNIDADES:
        vagas = api_get(f"unidades/posto-configurado-agenda/{uid}/{TIPO_SERVICO_ID}")
        if vagas is not None:
            return "vaga", (uid, nome, vagas, req_data)

    return "sem_vaga", req_data

# === BROWSER ===
async def fill_and_submit(page):
    await page.wait_for_timeout(2000)
    print("[form] Selecionando sistema: Migração")
    trigger = page.locator('p-dropdown[formcontrolname="sistema"] .ui-dropdown-trigger')
    await trigger.wait_for(state="visible", timeout=15000)
    await trigger.click()
    await page.wait_for_timeout(1000)
    opt = page.locator('.ui-dropdown-item:has-text("Migração")').first
    await opt.wait_for(state="visible", timeout=5000)
    await opt.click()
    await page.wait_for_timeout(500)
    cod = page.locator('input[maxlength="30"]').first
    await cod.fill(CODIGO_SOLICITACAO)
    nasc = page.locator('input[placeholder="Data de nascimento"]')
    await nasc.fill(DATA_NASCIMENTO)
    print("[form] Preenchido. Resolva o Turnstile no navegador...")
    await page.wait_for_selector('[name="cf-turnstile-response"]', state="attached", timeout=30000)
    await page.wait_for_function(
        '() => document.querySelector(\'[name="cf-turnstile-response"]\')?.value !== ""',
        timeout=120000)
    print("[form] Turnstile resolvido!")
    btn = page.locator('button.button-salvar').first
    await btn.wait_for(state="visible", timeout=10000)
    await btn.click()
    print("[form] Prosseguir clicado.")

async def book_with_browser(unit_id, unit_name, vagas_data, req_data):
    print(f"\n[book] Abrindo navegador para agendar em: {unit_name}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="pt-BR")
        page = await ctx.new_page()
        await page.goto(URL_ACCESS, wait_until="domcontentloaded", timeout=60000)
        await fill_and_submit(page)
        try:
            await page.wait_for_url("**/horario**", timeout=60000)
        except PwTimeout:
            notify("Não conseguiu acessar página de horários. Tente manualmente.", urgent=True)
            return
        notify(f"Navegador pronto! Faça o agendamento em {unit_name}", urgent=True)
        print("[book] Navegador aberto. Faça o agendamento manualmente.")
        print("[book] Pressione Ctrl+C para fechar quando terminar.")
        while True:
            await asyncio.sleep(60)

# === MAIN ===
def main():
    parser = argparse.ArgumentParser(description="Robô PF Agendamento")
    parser.add_argument("--interval", type=int, default=300, help="Intervalo entre verificações (segundos)")
    parser.add_argument("--notify-start", action="store_true", default=True, help="Notificar ao iniciar")
    args = parser.parse_args()

    print("=" * 60)
    print("  ROBÔ PF - AGENDAMENTO AUTOMÁTICO")
    print("=" * 60)
    print(f"  Código: {CODIGO_SOLICITACAO}")
    print(f"  Intervalo: {args.interval}s")
    print(f"  Notificação: ntfy.sh/{NTFY_TOPIC}")
    print("=" * 60)

    if args.notify_start:
        notify("🚀 Robô PF iniciado! Monitorando a cada {}s".format(args.interval), urgent=False)

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%H:%M:%S")
        status, data = check_api()

        if status == "vaga":
            uid, nome, vagas, req = data
            cod_val = req.get("codigoValidacao", "???")
            msg = f"VAGA ENCONTRADA em {nome}! Código: {cod_val}"
            notify(msg, urgent=True)
            print(f"\n[AGENDAR] Abrindo navegador...")
            asyncio.run(book_with_browser(uid, nome, vagas, req))
            break

        elif status == "ja_agendado":
            notify("Você já possui um agendamento!", urgent=True)
            break

        elif status == "requerente_nao_encontrado":
            notify("Requerente não encontrado! Verifique os dados.", urgent=True)
            break

        else:
            print(f"[{now}] #{attempt} Sem vagas. Próxima verificação em {args.interval}s")
            sys.stdout.flush()
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
