#!/usr/bin/env python3
"""
Monitor de Agendamento - Polícia Federal
Monitora via API pública a disponibilidade de vagas.
Uso: python3 monitor_api.py [--interval 60]
"""

import json
import time
import sys
import os
import subprocess
import datetime
import argparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://servicos.pf.gov.br/agenda-publico-rest/api"

CODIGO_SOLICITACAO = "202511201800457713"
DATA_NASCIMENTO = "2006-05-03"
SISTEMA_ID = 1  # 1=Migração
TIPO_SERVICO_ID = 11  # Autorização de Residência (Outras Hipóteses)
UNIDADE_ID = 13  # PAE/DPF/CAS/SP - Viracopos

UNIDADES_VISINHAS = [
    (13, "PAE Viracopos (Campinas)"),
    (531, "NUMIG Viracopos (Campinas)"),
    (400, "NO Campinas"),
    (502, "DELEX Campinas"),
    (134, "DPF Sorocaba"),
]

REQUERENTE_ID = None
CODIGO_VALIDACAO = None
REQUERENTE_DATA = None

def api_get(path):
    url = f"{BASE_URL}/{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            if not body:
                return None
            try:
                return json.loads(body) if body else None
            except json.JSONDecodeError:
                return body
    except HTTPError as e:
        return None
    except URLError:
        return None

def consultar_requerente():
    global REQUERENTE_ID, CODIGO_VALIDACAO, REQUERENTE_DATA
    path = f"agendamento/buscarRequerente/{DATA_NASCIMENTO}/{CODIGO_SOLICITACAO}/{SISTEMA_ID}"
    data = api_get(path)
    if data and isinstance(data, dict) and "id" in data:
        REQUERENTE_ID = data["id"]
        CODIGO_VALIDACAO = data.get("codigoValidacao")
        REQUERENTE_DATA = data
        return data
    return None

def consultar_agendamento():
    path = f"agendamento/buscarAgendamento?dataNascimento={DATA_NASCIMENTO}&codigoSolicitacao={CODIGO_SOLICITACAO}"
    return api_get(path)

def check_unit_availability(unit_id, tipo_servico_id):
    """Returns True if unit seems to have availability"""
    config = api_get(f"unidades/posto-configurado/{unit_id}/{tipo_servico_id}")
    if config is not None:
        return True, config
    vagas = api_get(f"unidades/posto-configurado-agenda/{unit_id}/{tipo_servico_id}")
    if vagas is not None:
        return True, vagas
    return False, None

NTFY_TOPIC = "pfagendamento_script"

def notify(msg, urgent=True):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[{ts}] {msg}")
    print(f"{'='*60}\n")
    print("\a", end="", flush=True)

    try:
        level = "critical" if urgent else "normal"
        subprocess.run(["notify-send", "PF Agendamento", msg, "-u", level],
                      timeout=5, capture_output=True)
    except:
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

def check_all_units():
    results = []
    for uid, nome in UNIDADES_VISINHAS:
        available, data = check_unit_availability(uid, TIPO_SERVICO_ID)
        unit_req = None
        if REQUERENTE_DATA:
            pre_units = REQUERENTE_DATA.get("listaPreAgendamentoUnidades", [])
            for pu in pre_units:
                if pu.get("idUnidade") == uid or uid == 13:
                    unit_req = pu
        results.append((uid, nome, available, data))
    return results

def main():
    global REQUERENTE_ID, CODIGO_VALIDACAO, REQUERENTE_DATA

    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60, help="Intervalo entre verificações (segundos)")
    parser.add_argument("--once", action="store_true", help="Verificar apenas uma vez")
    args = parser.parse_args()

    print("=" * 60)
    print("  MONITOR DE AGENDAMENTO - POLÍCIA FEDERAL")
    print("=" * 60)

    print(f"\n📋 Dados do Requerente:")
    print(f"   Código: {CODIGO_SOLICITACAO}")
    print(f"   Nascimento: {DATA_NASCIMENTO}")
    print(f"   Sistema: Migração")

    req = consultar_requerente()
    if not req:
        notify("❌ Requerente não encontrado!", urgent=True)
        sys.exit(1)

    print(f"   Nome: {req.get('nomeRequerente')}")
    print(f"   CPF: {req.get('cpf')}")
    print(f"   Email: {req.get('email')}")
    print(f"   Cidade: {req.get('cidade')}/{req.get('uf')}")
    print(f"   Serviço: {req.get('tipoServico', {}).get('nome', 'N/A')}")
    print(f"   Cod. Validação: {CODIGO_VALIDACAO}")

    ag = consultar_agendamento()
    if ag == "HAS_AGENDAMENTO":
        print("\n⚠ Você já possui um agendamento!")
        sys.exit(1)
    print("\n✅ Sem agendamento existente - pode agendar")

    pre_units = req.get("listaPreAgendamentoUnidades", [])
    if pre_units:
        print("\n📌 Unidades do pré-agendamento:")
        for pu in pre_units:
            print(f"   • ID {pu.get('idUnidade')} (ativa: {pu.get('ativo')})")

    print(f"\n🔍 Unidades sendo monitoradas:")
    for uid, nome in UNIDADES_VISINHAS:
        print(f"   • ID {uid}: {nome}")

    print(f"\n{'='*60}")
    print("🔄 MONITORANDO... Intervalo: {}s | Ctrl+C para parar".format(args.interval))
    print(f"{'='*60}")

    attempt = 0
    found = False

    try:
        while True:
            attempt += 1
            now = datetime.datetime.now().strftime("%H:%M:%S")

            results = check_all_units()
            any_available = False

            for uid, nome, available, data in results:
                if available:
                    any_available = True
                    msg = f"✅ {nome} (ID {uid}) pode ter vagas! Dados: {str(data)[:200]}"
                    notify(msg)
                    print(f"   Link: https://servicos.pf.gov.br/agenda-web/acessar")

            if not any_available:
                status = " | ".join([f"{nome.split()[0]}: sem vagas" for _, nome, _, _ in results])
                print(f"[{now}] #{attempt}: {status}")
            else:
                found = True

            if args.once:
                break

            sys.stdout.flush()
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nMonitoramento interrompido.")
        sys.exit(0)

if __name__ == "__main__":
    main()
