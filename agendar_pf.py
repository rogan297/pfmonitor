#!/usr/bin/env python3
import asyncio
import json
import sys
import time
import datetime
import subprocess
import argparse
from urllib.request import Request, urlopen
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# === CONFIG ===
URL_ACCESS = "https://servicos.pf.gov.br/agenda-web/acessar"
CODIGO_SOLICITACAO = "202512301318561835"
DATA_NASCIMENTO = "26/06/2000"
SISTEMA = "Migração"
CODIGO_VALIDACAO = "26F0E1C9FE737CB9B00ACBC4AB4FF6B0"

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
        subprocess.run(
            ["notify-send", "PF Agendamento", msg, "-u", level],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass

    try:
        data = json.dumps({
            "topic": NTFY_TOPIC,
            "title": "PF Agendamento",
            "message": msg,
            "priority": 5 if urgent else 3,
            "tags": ["rotating_light" if urgent else "bell"],
        }).encode()
        req = Request("https://ntfy.sh", data=data,
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
    except Exception as e:
        print(f"   ntfy.sh error: {e}")


# === HELPERS ===
async def fill_form(page):
    """Fill the access form and wait for Turnstile to be solved."""
    await page.wait_for_timeout(2000)

    print("[form] Selecionando sistema:", SISTEMA)
    dropdown_trigger = page.locator('p-dropdown[formcontrolname="sistema"] .ui-dropdown-trigger')
    await dropdown_trigger.wait_for(state="visible", timeout=15000)
    await dropdown_trigger.click()
    await page.wait_for_timeout(1000)

    migracao_option = page.locator('.ui-dropdown-item:has-text("Migração"), .ui-dropdown-item:has-text("Migracao")').first
    await migracao_option.wait_for(state="visible", timeout=5000)
    await migracao_option.click()
    await page.wait_for_timeout(500)
    print("[form] Sistema selecionado")

    cod_input = page.locator('input[placeholder*="Código"], input[maxlength="30"]').first
    await cod_input.fill(CODIGO_SOLICITACAO)

    nasc_input = page.locator('input[placeholder="Data de nascimento"]')
    await nasc_input.fill(DATA_NASCIMENTO)

    print("[form] Formulário preenchido. Resolva o Turnstile no navegador...")

    await page.wait_for_selector(
        '[name="cf-turnstile-response"]',
        state="attached", timeout=30000,
    )

    # Wait until Turnstile is solved (hidden input gets a value)
    await page.wait_for_function(
        '() => document.querySelector(\'[name="cf-turnstile-response"]\') && document.querySelector(\'[name="cf-turnstile-response"]\').value !== ""',
        timeout=120000,
    )
    print("[form] Turnstile resolvido!")


async def click_prosseguir(page):
    """Click the Prosseguir button and wait for navigation."""
    btn = page.locator('button.button-salvar.btn, button:has-text("Prosseguir")').first
    await btn.wait_for(state="visible", timeout=10000)
    await btn.click()
    print("[form] Botão Prosseguir clicado.")


async def fill_and_submit(page):
    await fill_form(page)
    await click_prosseguir(page)


# === MONITOR MODE ===
async def monitor_mode(page, interval=60):
    """Wait for navigation to /horario, then poll for available slots."""
    print("[monitor] Aguardando navegação para página de horários...")
    try:
        await page.wait_for_url("**/horario**", timeout=60000)
    except PwTimeout:
        # Maybe it stayed on the same page — check for error messages
        body = await page.text_content("body")
        if "não encontrado" in (body or "").lower():
            notify("Requerente não encontrado! Verifique os dados.")
        else:
            notify("Não foi possível acessar a página de horários.")
        return

    print(f"[monitor] Página de horários carregada: {page.url}")
    notify("Página de horários acessada! Iniciando monitoramento.")

    # Detect the fragment identifier on the refreshed page
    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            await page.reload(timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            print(f"[{now}] [#{attempt}] Erro ao recarregar: {e}")
            await asyncio.sleep(interval)
            continue

        # Look for available slot indicators
        slots_found = await check_for_slots(page)
        if slots_found:
            notify("VAGA DISPONÍVEL ENCONTRADA! Corra para agendar!", urgent=True)
            # Keep checking every 5s after first find
            await asyncio.sleep(5)
        else:
            print(f"[{now}] [#{attempt}] Nenhuma vaga disponível.")
            await asyncio.sleep(interval)


# === BOOK MODE ===
async def book_mode(page, interval=60):
    """Monitor and automatically book the first available slot."""
    print("[book] Aguardando navegação para página de horários...")
    try:
        await page.wait_for_url("**/horario**", timeout=60000)
    except PwTimeout:
        body = await page.text_content("body")
        if "não encontrado" in (body or "").lower():
            notify("Requerente não encontrado! Verifique os dados.")
        else:
            notify("Não foi possível acessar a página de horários.")
        return

    print(f"[book] Página de horários carregada: {page.url}")
    notify("Página de horários acessada! Iniciando monitoramento para agendamento automático.")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            await page.reload(timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            print(f"[{now}] [#{attempt}] Erro ao recarregar: {e}")
            await asyncio.sleep(interval)
            continue

        slot = await find_first_slot(page)
        if slot:
            notify(f"Vaga encontrada! Agendando: {slot}", urgent=True)
            try:
                await slot.click()
                # Look for confirm button
                confirm_btn = page.locator(
                    'button:has-text("Confirmar"), button:has-text("Agendar"), .button-salvar'
                ).first
                await confirm_btn.wait_for(state="visible", timeout=10000)
                await confirm_btn.click()
                await asyncio.sleep(3)
                notify("AGENDAMENTO REALIZADO COM SUCESSO!", urgent=True)
                print(f"[book] URL final: {page.url}")
                # Keep the browser open so user can see
                print("[book] Navegador mantido aberto. Pressione Ctrl+C para fechar.")
                while True:
                    await asyncio.sleep(60)
            except Exception as e:
                notify(f"Erro ao tentar agendar: {e}", urgent=True)
                print(f"[book] Exceção: {e}")
        else:
            print(f"[{now}] [#{attempt}] Nenhuma vaga disponível.")
            await asyncio.sleep(interval)


# === SLOT DETECTION ===
async def check_for_slots(page):
    """Return True if any slot seems available on the page."""
    # Common patterns for slot elements
    selectors = [
        'a:has-text("Disponível")',
        'a:has-text("Agendar")',
        'button:has-text("Disponível")',
        'button:has-text("Agendar")',
        '.agenda-item:not(.indisponivel)',
        'tr:has(td) a:not(.disabled)',
        '[class*="vaga"]',
        '[class*="disponivel"]',
        '.horario-disponivel',
        'input[type="radio"]:not([disabled])',
        'input[type="checkbox"]:not([disabled])',
    ]
    for sel in selectors:
        count = await page.locator(sel).count()
        if count > 0:
            print(f"[slots] Seletor '{sel}' encontrou {count} resultado(s).")
            return True
    return False


async def find_first_slot(page):
    """Return the locator for the first clickable slot, or None."""
    selectors = [
        'a:has-text("Disponível")',
        'a:has-text("Agendar")',
        'button:has-text("Disponível")',
        'button:has-text("Agendar")',
        '[class*="vaga"] a, [class*="disponivel"] a, .horario-disponivel',
        'input[type="radio"]:not([disabled])',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc
    return None


# === MAIN ===
async def main(args):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="pt-BR",
        )
        page = await context.new_page()

        print(f"[main] Acessando {URL_ACCESS} ...")
        await page.goto(URL_ACCESS, wait_until="domcontentloaded", timeout=60000)

        await fill_and_submit(page)

        if args.mode == "monitor":
            await monitor_mode(page, interval=args.interval)
        else:
            await book_mode(page, interval=args.interval)

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Automação de agendamento na Polícia Federal via Playwright"
    )
    parser.add_argument(
        "--mode",
        choices=["monitor", "book"],
        default="monitor",
        help="Modo de operação: monitor (notificar) ou book (agendar automaticamente)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalo entre verificações na página de horários (segundos)",
    )
    args = parser.parse_args()

    asyncio.run(main(args))
